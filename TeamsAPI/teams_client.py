import re
import requests

SKYPE_TOKEN_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
MSG_BASE = "https://amer.ng.msg.teams.microsoft.com/v1"

class TeamsClient:
    def __init__(self, msal_token):
        self.msal_token = msal_token
        self.skype_token = self._get_skype_token()
        self.headers = {
            "Authentication": f"skypetoken={self.skype_token}",
            "Accept": "application/json"
        }

    def _get_skype_token(self):
        res = requests.post(SKYPE_TOKEN_URL, headers={"Authorization": f"Bearer {self.msal_token}"}, timeout=15)
        if not res.ok:
            raise RuntimeError(f"Error obteniendo SkypeToken ({res.status_code}): {res.text}")
        return res.json()["tokens"]["skypeToken"]

    def list_teams(self):
        # Fetch conversations list and extract team spaces
        res = requests.get(f"{MSG_BASE}/users/ME/conversations?view=msnp24Equivalent&pageSize=50", headers=self.headers, timeout=15)
        if not res.ok:
            raise RuntimeError(f"Error al listar conversaciones ({res.status_code}): {res.text}")
        
        teams = []
        seen_topics = set()
        for c in res.json().get("conversations", []):
            cid = c.get("id", "")
            # tacv2 threads are Teams channels
            if cid.endswith("@thread.tacv2"):
                # Fetch thread detail for name
                try:
                    tr = requests.get(f"{MSG_BASE}/threads/{cid}", headers=self.headers, timeout=10)
                    if tr.ok:
                        props = tr.json().get("properties", {})
                        topic = props.get("spaceThreadTopic") or props.get("description") or cid
                        if topic not in seen_topics:
                            seen_topics.add(topic)
                            teams.append({
                                "id": cid,
                                "groupId": props.get("groupId"),
                                "displayName": topic,
                                "description": props.get("description", "")
                            })
                except Exception:
                    pass
            elif cid.endswith("@thread.v2") and ("meeting" in cid or "space" in cid):
                tprop = c.get("threadProperties", {})
                topic = tprop.get("topic") or c.get("properties", {}).get("topic")
                if topic and topic not in seen_topics:
                    seen_topics.add(topic)
                    teams.append({
                        "id": cid,
                        "groupId": None,
                        "displayName": topic,
                        "description": "Reunion o grupo de clase"
                    })
        return teams

    def get_messages(self, thread_id, page_size=10):
        url = f"{MSG_BASE}/users/ME/conversations/{thread_id}/messages?pageSize={page_size}"
        res = requests.get(url, headers=self.headers, timeout=15)
        if not res.ok:
            raise RuntimeError(f"Error al obtener mensajes ({res.status_code}): {res.text}")
        
        messages = []
        for m in res.json().get("messages", []):
            mtype = m.get("messagetype", "")
            if mtype in ["Message", "RichText/Html", "RichText"]:
                sender = m.get("imdisplayname") or "Desconocido"
                orig_time = m.get("originalarrivaltime") or m.get("composetime") or ""
                content = m.get("content") or ""
                clean_content = re.sub(r'<[^>]+>', ' ', content)
                clean_content = " ".join(clean_content.split())
                if clean_content:
                    messages.append({
                        "id": m.get("id"),
                        "from": sender,
                        "time": orig_time[:19].replace("T", " "),
                        "content": clean_content
                    })
        return messages
