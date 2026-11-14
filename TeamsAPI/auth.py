import os
import atexit
import msal

CACHE_FILE = os.path.join(os.path.dirname(__file__), "token_cache.bin")

# Microsoft Teams Official Client ID
CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
AUTHORITY = "https://login.microsoftonline.com/common"

SCOPES = [
    "https://api.spaces.skype.com/.default"
]

def get_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except Exception:
            pass

    def save_cache():
        if cache.has_state_changed:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(cache.serialize())

    atexit.register(save_cache)
    return cache

def get_msal_app():
    cache = get_cache()
    return msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )

def get_token(interactive=False):
    app = get_msal_app()
    accounts = app.get_accounts()
    
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    if not interactive:
        return None

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError("No se pudo iniciar el Device Flow: " + str(flow))

    code = flow['user_code']
    print(f"\nCODIGO TEAMS: {code}\n", flush=True)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        print("[OK] Sesion iniciada correctamente.", flush=True)
        return result["access_token"]
    else:
        err = result.get("error_description", result.get("error"))
        raise RuntimeError(f"Error al autenticar: {err}")
