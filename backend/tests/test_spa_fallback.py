"""SPA static fallback: unknown /api paths must 404 (not 500); other unknown
paths serve the SPA shell."""


def test_unknown_api_path_is_404_not_500(client):
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404


def test_unknown_non_api_path_serves_spa_shell(client):
    r = client.get("/some/client/side/route")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
