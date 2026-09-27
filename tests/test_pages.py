"""The website: public pages, the signed-in app pages, redirects, error pages
and the security headers every HTML response carries."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.product_helpers import CSRF, make_user, new_client, signed_in

PUBLIC_PAGES = ["/", "/about", "/pricing", "/docs", "/privacy", "/terms", "/login", "/signup", "/forgot"]


def _nonce(response) -> str:
    match = re.search(r"'nonce-([A-Za-z0-9_\-+/=]+)'", response.headers["content-security-policy"])
    assert match, response.headers["content-security-policy"]
    return match.group(1)


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(product, path):
    response = new_client().get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "<title>" in page and "</html>" in page
    # the inline theme script runs only because it carries this response's nonce
    assert f'nonce="{_nonce(response)}"' in page
    assert response.headers["x-content-type-options"] == "nosniff"
    # versioned assets, so a deploy never serves a stale stylesheet
    assert re.search(r'/static/css/site\.css\?v=[0-9a-f]{10}"', page)


@pytest.mark.parametrize("path", ["/", "/about", "/pricing", "/privacy", "/terms", "/login", "/signup"])
def test_public_pages_no_longer_mention_zoom(product, path):
    assert "zoom" not in new_client().get(path).text.lower()


def test_each_page_gets_a_fresh_nonce(product):
    client = new_client()
    assert _nonce(client.get("/")) != _nonce(client.get("/"))


def test_home_hero_form_hands_the_link_to_the_dashboard(product):
    page = new_client().get("/").text
    assert '<form class="hero-form" action="/app" method="get">' in page
    assert 'name="url"' in page
    # the struck filler in the headline is decoration: screen readers skip it
    assert '<span class="cut-run" aria-hidden="true">' in page


def test_app_pages_redirect_to_login_and_keep_the_query(product):
    client = new_client()
    response = client.get("/app?url=https%3A%2F%2Fyoutu.be%2Fabc", follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    # decoded once by the login page, `next` is exactly the address asked for
    assert parse_qs(location.query)["next"] == ["/app?url=https%3A%2F%2Fyoutu.be%2Fabc"]
    for path in ("/app/keys", "/app/account", "/app/admin", "/app/jobs/0123456789abcdef0123456789abcdef"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")


def test_old_single_page_links_redirect_to_the_job_page(product):
    response = new_client().get("/app?job=abc123", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/app/jobs/abc123"


def test_login_page_only_redirects_to_same_site_paths(product):
    client = signed_in()
    assert client.get("/login?next=/app/keys", follow_redirects=False).headers["location"] == "/app/keys"
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
        assert client.get("/login", params={"next": evil}, follow_redirects=False).headers["location"] == "/app"


def test_signed_in_pages_render(product):
    client = signed_in()
    dashboard = client.get("/app")
    assert dashboard.status_code == 200
    assert "Hi Ada" in dashboard.text
    assert 'aria-current="page"' in dashboard.text
    for path, marker in (("/app/keys", "API keys"), ("/app/account", "Account settings")):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text
        assert "noindex" in response.text


def test_unverified_accounts_see_the_verify_banner(product):
    client = signed_in(verified=False)
    assert "Confirm your email" in client.get("/app").text
    assert "Confirm your email" not in signed_in("b@example.com").get("/app").text


def test_admin_page_is_hidden_from_regular_users(product):
    response = signed_in().get("/app/admin")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_admin_page_opens_for_admins(product):
    make_user("root@example.com", role="admin")
    client = new_client()
    assert client.post("/api/v1/auth/login", json={"email": "root@example.com", "password": "correct horse battery"},
                       headers=CSRF).status_code == 200
    response = client.get("/app/admin")
    assert response.status_code == 200
    assert "Admin" in response.text


def test_unknown_job_page_is_a_404(product):
    response = signed_in().get("/app/jobs/0123456789abcdef0123456789abcdef")
    assert response.status_code == 404
    assert "Job not found" in response.text


def test_unknown_page_renders_the_html_404(product):
    response = new_client().get("/no-such-page", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert "Error 404" in response.text


def test_bad_verify_and_reset_links_explain_themselves(product):
    client = new_client()
    assert "This link doesn't work" in html.unescape(client.get("/verify?token=nope").text)
    assert "This link has expired" in html.unescape(client.get("/reset?token=nope").text)


def test_legacy_stylesheet_and_favicon(product):
    client = new_client()
    response = client.get("/styles.css", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"].startswith("/static/css/site.css?v=")
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/svg+xml")


def test_robots_and_sitemap(product):
    client = new_client()
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /app" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<loc>" in sitemap.text and "/app" not in sitemap.text.replace("/api", "")


def test_static_files_the_pages_reference_exist(product):
    client = new_client()
    pages = client.get("/").text + client.get("/login").text
    for path in sorted(set(re.findall(r'"(/static/[^"?]+)\?v=', pages))):
        assert client.get(path).status_code == 200, path
