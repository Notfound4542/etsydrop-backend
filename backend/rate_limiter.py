# =====================================================================
# === RATE_LIMITER.PY — Instance Limiter partagée (slowapi) ===
# =====================================================================
#
# Extrait de main.py pour être importable depuis les routers : le webhook
# Stripe (routers/billing.py) doit être exempté du rate limit global via
# @limiter.exempt, ce qui exige la même instance Limiter que celle posée
# sur app.state par main.py.

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
