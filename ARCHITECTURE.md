
class AppContext:
    _services = {}

    @classmethod
    def register(cls, svc_type, instance):
        cls._services[svc_type] = instance

    @classmethod
    def get(cls, svc_type):
        return cls._services.get(svc_type)
