"""Built-in engine wiring. Add adapters here; Runner also accepts an explicit one."""
from engine import Engine
from engine_lichtfeld import LichtFeldEngine
from engine_spirula import SpirulaEngine


ENGINES: dict[str, Engine] = {'lichtfeld': LichtFeldEngine(), 'spirula': SpirulaEngine()}


def get_engine(name):
    try:
        return ENGINES[name]
    except (KeyError, TypeError):
        raise ValueError('Unknown scan backend') from None
