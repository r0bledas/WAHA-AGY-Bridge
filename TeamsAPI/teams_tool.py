import sys
import argparse
from auth import get_token
from teams_client import TeamsClient

def main():
    parser = argparse.ArgumentParser(description="Microsoft Teams CLI tool for Antigravity")
    parser.add_argument("--login", action="store_true", help="Iniciar sesion interactivamente con Device Code")
    parser.add_argument("--teams", action="store_true", help="Listar todos los equipos/clases/canales")
    parser.add_argument("--messages", action="store_true", help="Listar mensajes recientes de un equipo o reunion")
    parser.add_argument("--team", type=str, help="Nombre o ID del equipo / canal")
    parser.add_argument("--top", type=int, default=5, help="Numero de mensajes a obtener")
    
    args = parser.parse_args()

    interactive = args.login
    try:
        token = get_token(interactive=interactive)
    except Exception as e:
        print(f"Error de autenticacion: {e}")
        sys.exit(1)

    if not token:
        print("Sesion no iniciada o token expirado. Ejecuta: python teams_tool.py --login")
        sys.exit(1)

    if args.login:
        print("Login verificado con exito.")
        return

    try:
        client = TeamsClient(token)
    except Exception as e:
        print(f"Error conectando a Teams: {e}")
        return

    if args.teams:
        print("Consultando tus materias y canales en Teams...")
        try:
            teams = client.list_teams()
            if not teams:
                print("No se encontraron materias o canales disponibles.")
                return
            print("MATERIAS Y EQUIPOS EN TEAMS:")
            for i, t in enumerate(teams, 1):
                print(f"{i}. {t['displayName']}")
                print(f"   ID: {t['id']}")
        except Exception as e:
            print(f"Error al listar equipos: {e}")

    elif args.messages:
        if not args.team:
            print("Debes especificar el equipo con --team \"<nombre o ID>\"")
            return
        
        target_id = args.team
        if not (args.team.startswith("19:") or "@thread" in args.team):
            # Search by name
            teams = client.list_teams()
            matched = [t for t in teams if args.team.lower() in t["displayName"].lower()]
            if not matched:
                print(f"No se encontro ninguna materia que coincida con '{args.team}'")
                return
            target_id = matched[0]["id"]
            print(f"Canal seleccionado: {matched[0]['displayName']}")

        try:
            msgs = client.get_messages(target_id, page_size=args.top)
            if not msgs:
                print("No hay mensajes recientes en esta conversacion.")
                return
            print(f"MENSAJES RECIENTES ({len(msgs)}):")
            for m in msgs:
                print(f"[{m['time']}] {m['from']}:")
                print(f"{m['content']}\n")
        except Exception as e:
            print(f"Error al obtener mensajes: {e}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
