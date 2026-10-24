"""Sign-in page."""
from __future__ import annotations

from fasthtml.common import Article, Form, H1, Header, Input, Main, P, Small, Span, Strong, Title

from app.ui.core import APP_NAME, btn, field, icon, notice


def login_page(error: str | None = None, not_configured: bool = False):
    if not_configured:
        body = notice("warn", Strong("APP_PASSWORD is not set"),
                      P("Set APP_PASSWORD in the container environment and restart."))
    else:
        body = Form(field("Password", Input(type="password", id="password", name="password", autofocus=True,
                                            required=True, autocomplete="current-password",
                                            aria_invalid="true" if error else None,
                                            aria_describedby="login-error" if error else None), fr="password"),
                    Small(error, id="login-error", role="alert") if error else None,
                    btn("Sign in", type="submit", cls="block"),
                    action="/login", method="post")
    return (Title(f"Sign in · {APP_NAME}"),
            Main(Article(Header(Span(icon("music"), cls="brand-mark lg"), H1(APP_NAME),
                                P("Sign in to continue", cls="muted")),
                         body, cls="login-card"),
                 cls="container login-wrap"))
