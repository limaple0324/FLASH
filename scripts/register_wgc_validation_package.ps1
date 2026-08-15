[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExternalLocation,

    [Parameter(Mandatory = $true)]
    [switch]$ValidationOnly,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9A-Fa-f]{64}$")]
    [string]$ExpectedSha256,

    [string]$OutputDirectory = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $ValidationOnly) {
    throw "本腳本只允許目前驗證電腦使用，必須明確指定 -ValidationOnly。"
}

$repositoryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..")
)
$externalRoot = [System.IO.Path]::GetFullPath($ExternalLocation)
$flashExecutable = Join-Path $externalRoot "FLASH.exe"
if (-not (Test-Path -LiteralPath $flashExecutable -PathType Leaf)) {
    throw "外部位置缺少 FLASH.exe，拒絕註冊。"
}

$expectedHash = $ExpectedSha256.Trim().ToUpperInvariant()
$actualHash = (
    Get-FileHash -LiteralPath $flashExecutable -Algorithm SHA256
).Hash.ToUpperInvariant()
if ($actualHash -ne $expectedHash) {
    throw "驗證用 FLASH.exe 雜湊不符，未建立或註冊套件。"
}

$manifest = Join-Path $repositoryRoot (
    "packaging\wgc-validation\Package.appxmanifest"
)
$logo = Join-Path $repositoryRoot "assets\flash_icon.png"
if (
    -not (Test-Path -LiteralPath $manifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $logo -PathType Leaf)
) {
    throw "驗證套件來源不完整，拒絕註冊。"
}

$programFilesX86 = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::ProgramFilesX86
)
$sdkBinRoot = Join-Path $programFilesX86 "Windows Kits\10\bin"
$sdkTools = Get-ChildItem -LiteralPath $sdkBinRoot -Directory |
    Sort-Object Name -Descending |
    ForEach-Object {
        $makeAppx = Join-Path $_.FullName "x64\makeappx.exe"
        $signTool = Join-Path $_.FullName "x64\signtool.exe"
        if (
            (Test-Path -LiteralPath $makeAppx -PathType Leaf) -and
            (Test-Path -LiteralPath $signTool -PathType Leaf)
        ) {
            [PSCustomObject]@{
                MakeAppx = $makeAppx
                SignTool = $signTool
            }
        }
    } |
    Select-Object -First 1
if ($null -eq $sdkTools) {
    throw "找不到 Windows SDK 套件與簽署工具。"
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path (
        [Environment]::GetFolderPath("LocalApplicationData")
    ) "FLASH\wgc-validation-package"
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$runName = Get-Date -Format "yyyyMMdd-HHmmss"
$runRoot = Join-Path $outputRoot $runName
$stageRoot = Join-Path $runRoot "package"
$assetsRoot = Join-Path $stageRoot "Assets"
[System.IO.Directory]::CreateDirectory($assetsRoot) | Out-Null

Copy-Item -LiteralPath $manifest -Destination (
    Join-Path $stageRoot "AppxManifest.xml"
)
foreach ($assetName in @(
    "StoreLogo.png",
    "Square150x150Logo.png",
    "Square44x44Logo.png"
)) {
    Copy-Item -LiteralPath $logo -Destination (
        Join-Path $assetsRoot $assetName
    )
}

$packagePath = Join-Path $runRoot "FLASH-WGC-Validation.msix"
& $sdkTools.MakeAppx pack /d $stageRoot /p $packagePath /o /nv
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $packagePath)) {
    throw "驗證套件建立失敗。"
}

$certificateSubject = "CN=limaple0324 FLASH WGC Validation"
$certificate = Get-ChildItem -LiteralPath Cert:\CurrentUser\My |
    Where-Object {
        $_.Subject -eq $certificateSubject -and
        $_.HasPrivateKey -and
        $_.NotAfter -gt (Get-Date).AddDays(1)
    } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1
if ($null -eq $certificate) {
    $certificate = New-SelfSignedCertificate `
        -Type Custom `
        -Subject $certificateSubject `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -KeyUsage DigitalSignature `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddDays(30) `
        -TextExtension @(
            "2.5.29.19={critical}{text}ca=false",
            "2.5.29.37={critical}{text}1.3.6.1.5.5.7.3.3"
        )
}

$certificatePath = Join-Path $runRoot "FLASH-WGC-Validation.cer"
Export-Certificate -Cert $certificate -FilePath $certificatePath -Force |
    Out-Null
Import-Certificate `
    -FilePath $certificatePath `
    -CertStoreLocation Cert:\CurrentUser\TrustedPeople |
    Out-Null
Import-Certificate `
    -FilePath $certificatePath `
    -CertStoreLocation Cert:\CurrentUser\Root |
    Out-Null

& $sdkTools.SignTool sign `
    /fd SHA256 `
    /sha1 $certificate.Thumbprint `
    /s My `
    $packagePath
if ($LASTEXITCODE -ne 0) {
    throw "驗證套件簽署失敗。"
}

Add-AppxPackage -Path $packagePath -ExternalLocation $externalRoot
$registered = Get-AppxPackage -Name "limaple0324.FLASH.WgcValidation"
if ($null -eq $registered) {
    throw "驗證套件註冊後無法讀回，已視為失敗。"
}

[PSCustomObject]@{
    ValidationOnly = $true
    CommercialUse = $false
    FormalUpdate = $false
    PackagePath = $packagePath
    ExternalLocation = $externalRoot
    PackageFamilyName = $registered.PackageFamilyName
    LaunchIdentity = "$($registered.PackageFamilyName)!FLASH"
}
