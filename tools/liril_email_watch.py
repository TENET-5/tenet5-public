"""liril_email_watch — checks gmail_credentials.json every 60s and runs
a real SMTP HELO/AUTH against Gmail. Publishes pass/fail to
data/liril_thoughts.jsonl so user can see in real time when creds work.

Does NOT send any email. Just authenticates. If AUTH succeeds, the
broadcaster on its next cycle will start sending.
"""
import json, time, smtplib, os
from pathlib import Path

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
SITE  = Path(r"E:\TENET-5.github.io")
CREDS = SLATE / "tools" / "lirilclaw" / "gmail_credentials.json"
THOUGHTS = SITE / "data" / "liril_thoughts.jsonl"
STATE = SLATE / "data" / "email_watch_state.json"


def log(kind, summary, severity="medium", **extra):
    t = {"ts": int(time.time()), "kind": kind, "source": "email_watch",
         "summary": summary, "severity": severity, **extra}
    try:
        THOUGHTS.parent.mkdir(parents=True, exist_ok=True)
        with THOUGHTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except Exception:
        pass


def looks_like_app_password(s: str) -> bool:
    """Gmail app passwords are 16 chars, alnum only (no punctuation).
    Google displays them with spaces: 'abcd efgh ijkl mnop'. We accept
    with or without spaces."""
    stripped = (s or "").replace(" ", "")
    return len(stripped) == 16 and stripped.isalnum()


def load_creds():
    if not CREDS.exists():
        return None, "credentials file missing"
    try:
        d = json.loads(CREDS.read_text(encoding="utf-8"))
        return d, None
    except Exception as e:
        return None, f"unparseable: {e}"


def try_auth(user: str, pw: str) -> tuple[bool, str]:
    pw = pw.replace(" ", "")
    try:
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15)
        s.login(user, pw)
        s.quit()
        return True, "auth OK"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"auth failed: {e.smtp_code} {e.smtp_error}"
    except Exception as e:
        return False, f"connect failed: {e!r}"


def main():
    last_state = None
    log("observation", "email_watch online — will SMTP-auth-test every 60s", "low")
    while True:
        creds, err = load_creds()
        if err:
            if last_state != ("error", err):
                log("alert", f"email config problem: {err}", "high")
                last_state = ("error", err)
        else:
            user = creds.get("user") or ""
            pw = creds.get("app_password") or ""
            if not looks_like_app_password(pw):
                if last_state != ("wrong-format", pw[:4]):
                    log("alert",
                        f"{user}: password value is NOT a 16-char Gmail app password. "
                        f"Current value looks like a regular account password — Gmail SMTP will reject. "
                        f"Generate a real app password at myaccount.google.com/apppasswords.",
                        "high",
                        password_format="regular-password-not-app-password")
                    last_state = ("wrong-format", pw[:4])
            else:
                ok, detail = try_auth(user, pw)
                key = ("auth", ok, detail[:40])
                if last_state != key:
                    if ok:
                        log("good",
                            f"{user}: Gmail SMTP AUTH succeeded. "
                            f"Broadcaster will start sending on next cycle.",
                            "high")
                    else:
                        log("alert",
                            f"{user}: SMTP auth failed even though password looks like app-format: {detail}",
                            "high")
                    last_state = key
        # Persist state for dashboard
        try:
            STATE.write_text(json.dumps({
                "last_checked_utc": int(time.time()),
                "last_state": str(last_state),
            }), encoding="utf-8")
        except Exception:
            pass
        time.sleep(60)


if __name__ == "__main__":
    main()
