import copy
from types import SimpleNamespace
from typing import Optional

import pytest

from tests.courseorganizer_testkit import load_courseorganizer

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient
    from fastapi import Depends
    from starlette.status import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
except Exception:
    pytest.skip("fastapi is required for route-level authorization tests", allow_module_level=True)


MODULE = load_courseorganizer()
CourseOrganizer = MODULE.CourseOrganizer


def _response_data(response):
    return response.data if hasattr(response, "data") else response["data"]


def _make_organizer(tmp_path):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movies"
    children_output = tmp_path / "children"

    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()
    (incoming / "课程").mkdir()
    (incoming / "课程" / "1.mp4").write_bytes(b"media")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "naming_mode": "preview",
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
        }
    )
    organizer.save_data(
        "naming_preview_v1",
        [
            {
                "raw_title": "课程",
                "final_title": "候选课程",
                "target_library": "tv",
                "target_output_root": str(tv_output / "课程"),
                "status": "review",
                "reason_codes": ["review_needed"],
                "timestamp": 1,
            }
        ],
    )
    return organizer


def _manual_payload():
    return {
        "schema": 1,
        "items": {
            "课程": {
                "action": "ignore",
                "final_title": "",
                "target_library": "",
                "updated_at": 1700000000,
                "source_revision": "keep",
            }
        },
    }


def _build_client(organizer, token_users):
    app = FastAPI()

    def get_current_user(authorization: Optional[str] = Header(default=None)):
        if authorization is None:
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="未登录")
        user = token_users.get(authorization)
        if user is None:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="token无效")
        return user

    def get_current_active_superuser(user=Depends(get_current_user)):
        if bool(hasattr(user, "is_active") and user.is_active is False):
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="用户权限不足",
            )
        if not bool(getattr(user, "is_superuser", False)):
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="用户权限不足",
            )
        return user

    app.dependency_overrides[MODULE.REVIEW_AUTH_DEPENDENCY] = (
        get_current_active_superuser
    )
    for api in organizer.get_api():
        app.add_api_route(
            api["path"],
            endpoint=api["endpoint"],
            methods=api["methods"],
        )

    return TestClient(app)


def _token_users():
    return {
        "Bearer normal": SimpleNamespace(is_active=True, is_superuser=False),
        "Bearer inactive": SimpleNamespace(is_active=False, is_superuser=True),
        "Bearer super": SimpleNamespace(is_active=True, is_superuser=True),
    }


@pytest.mark.parametrize(
    "token,status_code,expect_items",
    [
        (None, HTTP_401_UNAUTHORIZED, False),
        ("Bearer bad", HTTP_403_FORBIDDEN, False),
        ("Bearer normal", HTTP_400_BAD_REQUEST, False),
        ("Bearer inactive", HTTP_400_BAD_REQUEST, False),
        ("Bearer super", HTTP_200_OK, True),
    ],
    ids=["anonymous", "bad_token", "normal_active", "inactive", "superuser"],
)
def test_review_get_auth_matrix_http_chain(tmp_path, token, status_code, expect_items):
    organizer = _make_organizer(tmp_path)
    client = _build_client(organizer, _token_users())

    headers = {"Authorization": token} if token else {}
    response = client.get("/review", headers=headers)

    assert response.status_code == status_code
    body = response.json()
    if status_code == HTTP_200_OK:
        assert bool(body["data"].get("items")) == expect_items
    else:
        assert "success" not in body
        assert "课程" not in str(body)


@pytest.mark.parametrize(
    "token,status_code",
    [
        (None, HTTP_401_UNAUTHORIZED),
        ("Bearer bad", HTTP_403_FORBIDDEN),
        ("Bearer normal", HTTP_400_BAD_REQUEST),
        ("Bearer inactive", HTTP_400_BAD_REQUEST),
        ("Bearer super", HTTP_200_OK),
    ],
    ids=["anonymous", "bad_token", "normal_active", "inactive", "superuser"],
)
def test_review_post_auth_matrix_http_chain(tmp_path, token, status_code):
    organizer = _make_organizer(tmp_path)
    organizer.save_data(CourseOrganizer.MANUAL_DECISIONS_KEY, _manual_payload())
    baseline = copy.deepcopy(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY))
    row = _response_data(organizer.get_review())["items"][0]

    payload = {
        "raw_title": "课程",
        "revision": row["revision"],
        "action": "ignore",
    }

    client = _build_client(organizer, _token_users())
    headers = {"Authorization": token} if token else {}
    response = client.post("/review", json=payload, headers=headers)

    assert response.status_code == status_code
    body = response.json()
    if status_code == HTTP_200_OK:
        assert body["success"] is True
        assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]["action"] == "ignore"
        assert (tmp_path / "incoming" / "课程" / "1.mp4").is_file()
    else:
        assert body.get("success") is None
        assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) == baseline
        assert "课程" not in str(body)


def test_review_routes_fail_closed_when_host_guard_is_unavailable(
    tmp_path, monkeypatch
):
    organizer = _make_organizer(tmp_path)
    called = []
    monkeypatch.setattr(organizer, "get_review", lambda: called.append(True))
    app = FastAPI()
    app.add_api_route("/review", organizer.get_review_route, methods=["GET"])

    response = TestClient(app).get("/review")

    assert response.status_code == 503
    assert called == []


@pytest.mark.parametrize("path", ["/review/tmdb/search", "/review/tmdb/associate"])
def test_tmdb_routes_use_the_same_superuser_guard(tmp_path, path):
    organizer = _make_organizer(tmp_path)
    client = _build_client(organizer, _token_users())
    row = _response_data(organizer.get_review())["items"][0]
    payload = {
        "raw_title": "课程",
        "revision": row["revision"],
        "candidate_key": "themoviedb:1:movie",
    }
    assert client.post(path, json=payload).status_code == HTTP_401_UNAUTHORIZED
    assert client.post(path, json=payload, headers={"Authorization": "Bearer bad"}).status_code == HTTP_403_FORBIDDEN
    response = client.post(
        path,
        json=payload,
        headers={"Authorization": "Bearer super"},
    )
    assert response.status_code == HTTP_200_OK
