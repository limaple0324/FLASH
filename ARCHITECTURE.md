# FLASH Architecture

## SP1 Bootstrap v0.1

SP1 establishes the minimum stable engineering foundation for FLASH.

## Responsibility boundaries

- `main.py`: application entrypoint only.
- `core/`: startup coordination and future application lifecycle logic.
- `config/`: paths and persistent configuration.
- `services/`: shared infrastructure services such as context, event bus, and logging.
- `workspace/`: reserved boundary for SP2 Workspace integration.
- `plugins/`: reserved boundary for plugin-ready expansion.
- `tests/`: smoke tests and regression checks.

## Current architectural principles

- Keep startup simple and observable.
- Keep services registered through `AppContext`.
- Keep cross-module communication behind `EventBus`.
- Keep SP2 concepts out of SP1 implementation until the foundation is verified.
