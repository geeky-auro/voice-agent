"""Talk to the agent from the terminal, without WhatsApp.

    python cli.py                        # type messages
    python cli.py --audio note.ogg       # transcribe a voice note first (needs SARVAM_API_KEY)
    python cli.py --shop 919999999999    # pick which shop's data to use
"""

import argparse
import logging
import mimetypes

from app import agent, db, inventory, stt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--shop", default="demo-shop")
    p.add_argument("--audio", help="path to an audio file to transcribe and send")
    p.add_argument("--db", default=None, help="SQLite path (default: DB_PATH or shop.db)")
    p.add_argument("-v", "--verbose", action="store_true", help="log tool calls")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    db.connect(args.db)

    if args.audio:
        mime = mimetypes.guess_type(args.audio)[0] or "audio/ogg"
        with open(args.audio, "rb") as f:
            text = stt.transcribe(f.read(), mime, keyterms=inventory.product_names(args.shop))
        print(f"🎙️  {text}")
        print(agent.handle_message(args.shop, text))
        return

    print("Type a message (Ctrl-D to quit). e.g. '10 packet Maggi aaye, 3 bik gaye'")
    while True:
        try:
            text = input("\nyou> ").strip()
        except EOFError:
            break
        if text:
            print("bot>", agent.handle_message(args.shop, text))


if __name__ == "__main__":
    main()
