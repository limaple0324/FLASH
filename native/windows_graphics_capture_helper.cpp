#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <d3d11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Security.Authorization.AppCapabilityAccess.h>
#include <winrt/base.h>

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <mutex>

namespace {

using namespace winrt;
using namespace winrt::Windows::Graphics::Capture;
using namespace winrt::Windows::Graphics::DirectX;
using namespace winrt::Windows::Graphics::DirectX::Direct3D11;
using winrt::Windows::Security::Authorization::AppCapabilityAccess::
    AppCapabilityAccessStatus;

constexpr int kAccessAllowed = 1;
constexpr int kAccessDenied = 2;
constexpr int kAccessUnspecified = 3;
constexpr int kAccessError = -1;
constexpr int kCaptureOk = 1;
constexpr int kCaptureBufferTooSmall = 2;
constexpr int kCaptureInvalidArgument = -1;
constexpr int kCaptureAccessUnavailable = -2;
constexpr int kCaptureMinimized = -3;
constexpr int kCaptureBorderRequired = -4;
constexpr int kCaptureTimeout = -5;
constexpr int kCaptureError = -6;

std::mutex g_access_mutex;
int g_access_state = kAccessUnspecified;

struct FlashWgcFrame {
    std::uint32_t size;
    std::uint32_t width;
    std::uint32_t height;
    std::uint32_t stride;
    std::uint32_t required_bytes;
    std::uint64_t timestamp;
};

void ensure_apartment() {
    try {
        init_apartment(apartment_type::multi_threaded);
    } catch (hresult_error const& error) {
        if (error.code() != RPC_E_CHANGED_MODE) {
            throw;
        }
    }
}

IDirect3DDevice create_direct3d_device() {
    com_ptr<ID3D11Device> device;
    com_ptr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level{};
    HRESULT result = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        device.put(),
        &level,
        context.put());
    if (FAILED(result)) {
        check_hresult(D3D11CreateDevice(
            nullptr,
            D3D_DRIVER_TYPE_WARP,
            nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            nullptr,
            0,
            D3D11_SDK_VERSION,
            device.put(),
            &level,
            context.put()));
    }

    com_ptr<IDXGIDevice> dxgi_device;
    device.as(dxgi_device);
    com_ptr<IInspectable> inspectable;
    check_hresult(CreateDirect3D11DeviceFromDXGIDevice(
        dxgi_device.get(), inspectable.put()));
    return inspectable.as<IDirect3DDevice>();
}

GraphicsCaptureItem create_capture_item(HWND window) {
    auto interop = get_activation_factory<
        GraphicsCaptureItem,
        IGraphicsCaptureItemInterop>();
    GraphicsCaptureItem item{nullptr};
    check_hresult(interop->CreateForWindow(
        window,
        guid_of<GraphicsCaptureItem>(),
        put_abi(item)));
    return item;
}

std::uint64_t frame_timestamp(Direct3D11CaptureFrame const& frame) {
    auto count = frame.SystemRelativeTime().count();
    return count > 0 ? static_cast<std::uint64_t>(count) : 0;
}

int copy_frame(
    Direct3D11CaptureFrame const& frame,
    std::uint8_t* destination,
    std::uint32_t capacity,
    FlashWgcFrame* output) {
    auto access = frame.Surface().as<
        ::Windows::Graphics::DirectX::Direct3D11::
            IDirect3DDxgiInterfaceAccess>();
    com_ptr<ID3D11Texture2D> texture;
    check_hresult(access->GetInterface(
        __uuidof(ID3D11Texture2D), texture.put_void()));

    D3D11_TEXTURE2D_DESC description{};
    texture->GetDesc(&description);
    if (
        description.Width == 0 || description.Height == 0 ||
        description.Width > UINT32_MAX / 4 ||
        description.Height > UINT32_MAX / (description.Width * 4)) {
        return kCaptureError;
    }
    std::uint32_t stride = description.Width * 4;
    std::uint32_t required = stride * description.Height;
    output->width = description.Width;
    output->height = description.Height;
    output->stride = stride;
    output->required_bytes = required;
    output->timestamp = frame_timestamp(frame);
    if (destination == nullptr || capacity < required) {
        return kCaptureBufferTooSmall;
    }

    D3D11_TEXTURE2D_DESC staging_description = description;
    staging_description.BindFlags = 0;
    staging_description.MiscFlags = 0;
    staging_description.Usage = D3D11_USAGE_STAGING;
    staging_description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    com_ptr<ID3D11Texture2D> staging;
    com_ptr<ID3D11Device> device;
    texture->GetDevice(device.put());
    check_hresult(device->CreateTexture2D(
        &staging_description, nullptr, staging.put()));
    com_ptr<ID3D11DeviceContext> context;
    device->GetImmediateContext(context.put());
    context->CopyResource(staging.get(), texture.get());

    D3D11_MAPPED_SUBRESOURCE mapped{};
    check_hresult(context->Map(
        staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
    for (std::uint32_t row = 0; row < description.Height; ++row) {
        std::memcpy(
            destination + row * stride,
            static_cast<std::uint8_t const*>(mapped.pData) +
                row * mapped.RowPitch,
            stride);
    }
    context->Unmap(staging.get(), 0);
    return kCaptureOk;
}

}  // namespace

extern "C" __declspec(dllexport) std::uint32_t __stdcall
FlashWgcHelperAbiVersion() noexcept {
    return 1;
}

extern "C" __declspec(dllexport) int __stdcall
FlashWgcPrepareBorderlessAccess() noexcept {
    std::scoped_lock lock(g_access_mutex);
    try {
        ensure_apartment();
        auto status = GraphicsCaptureAccess::RequestAccessAsync(
            GraphicsCaptureAccessKind::Borderless).get();
        if (status == AppCapabilityAccessStatus::Allowed) {
            g_access_state = kAccessAllowed;
        } else if (status == AppCapabilityAccessStatus::DeniedBySystem ||
                   status == AppCapabilityAccessStatus::DeniedByUser) {
            g_access_state = kAccessDenied;
        } else {
            g_access_state = kAccessUnspecified;
        }
    } catch (...) {
        g_access_state = kAccessError;
    }
    return g_access_state;
}

extern "C" __declspec(dllexport) int __stdcall FlashWgcCaptureWindow(
    HWND window,
    std::uint64_t after_timestamp,
    std::uint32_t timeout_ms,
    std::uint8_t* destination,
    std::uint32_t capacity,
    FlashWgcFrame* output) noexcept {
    if (window == nullptr || output == nullptr ||
        output->size != sizeof(FlashWgcFrame) || !IsWindow(window)) {
        return kCaptureInvalidArgument;
    }
    output->width = 0;
    output->height = 0;
    output->stride = 0;
    output->required_bytes = 0;
    output->timestamp = 0;
    {
        std::scoped_lock lock(g_access_mutex);
        if (g_access_state != kAccessAllowed) {
            return kCaptureAccessUnavailable;
        }
    }
    if (IsIconic(window)) {
        return kCaptureMinimized;
    }

    try {
        ensure_apartment();
        auto item = create_capture_item(window);
        auto size = item.Size();
        if (size.Width <= 0 || size.Height <= 0) {
            return kCaptureError;
        }
        auto direct3d_device = create_direct3d_device();
        auto frame_pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            direct3d_device,
            DirectXPixelFormat::B8G8R8A8UIntNormalized,
            2,
            size);
        auto session = frame_pool.CreateCaptureSession(item);
        session.IsBorderRequired(false);
        if (session.IsBorderRequired()) {
            session.Close();
            frame_pool.Close();
            return kCaptureBorderRequired;
        }

        std::mutex frame_mutex;
        std::condition_variable frame_ready;
        bool signalled = false;
        auto token = frame_pool.FrameArrived(
            [&](Direct3D11CaptureFramePool const&,
                winrt::Windows::Foundation::IInspectable const&) {
                std::scoped_lock lock(frame_mutex);
                signalled = true;
                frame_ready.notify_one();
            });
        session.StartCapture();
        auto deadline = std::chrono::steady_clock::now() +
            std::chrono::milliseconds(timeout_ms == 0 ? 1 : timeout_ms);
        int result = kCaptureTimeout;
        while (std::chrono::steady_clock::now() < deadline) {
            {
                std::unique_lock lock(frame_mutex);
                frame_ready.wait_until(
                    lock, deadline, [&] { return signalled; });
                signalled = false;
            }
            while (auto frame = frame_pool.TryGetNextFrame()) {
                auto timestamp = frame_timestamp(frame);
                if (timestamp == 0 || timestamp <= after_timestamp) {
                    continue;
                }
                result = copy_frame(frame, destination, capacity, output);
                break;
            }
            if (result != kCaptureTimeout) {
                break;
            }
        }
        frame_pool.FrameArrived(token);
        session.Close();
        frame_pool.Close();
        return result;
    } catch (...) {
        return kCaptureError;
    }
}
