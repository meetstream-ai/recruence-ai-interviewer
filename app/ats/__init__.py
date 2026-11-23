"""External ATS integrations: provider adapters (app/ats/<provider>.py) and the service that feeds Recruence's one
recruitment pipeline from them and reports results back (app/ats/service.py). docs/ATS_INTEGRATIONS.md."""

from app.ats.base import Adapter
from app.ats.generic import GenericAdapter
from app.ats.greenhouse import GreenhouseAdapter
from app.ats.lever import LeverAdapter

ADAPTERS: dict[str, Adapter] = {a.provider: a for a in (GreenhouseAdapter(), LeverAdapter(), GenericAdapter())}
