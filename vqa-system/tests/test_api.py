"""API 层回归：认证/CSRF/锁定/改密、三态卡片契约、反馈权限、技术失败 503"""
import pytest

from app import llm
from app.models import User

PW = "Passw0rd123"
Q = "农村宅基地申请流程"
CONTENT = "农村宅基地申请由村民向村委会提出书面申请，经村委会审核公示后报乡镇人民政府审批。"


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ask_requires_login(client):
    r = client.post("/api/ask", json={"question": "你好"})
    assert r.status_code == 401


def test_csrf_required_for_write(client, login):
    login()
    r = client.post("/api/ask", json={"question": Q})    # 缺 X-CSRF-Token
    assert r.status_code == 403


def test_ask_validates_question_shape(client, login):
    csrf = login()
    r1 = client.post("/api/ask", json={"question": "   "}, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 400
    r2 = client.post("/api/ask", json={"question": "长" * 501}, headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 400


def test_ask_three_state_contracts(client, login, publish, run):
    csrf = login()
    run(publish(Q, CONTENT))

    # answered 卡片
    r = client.post("/api/ask", json={"question": Q}, headers={"X-CSRF-Token": csrf})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "answered"
    assert body["answer"] and body["citations"] and body["disclaimer"]

    # missed 卡片（无关问题）
    r2 = client.post("/api/ask", json={"question": "量子力学中的测不准原理是说什么的"},
                     headers={"X-CSRF-Token": csrf})
    b2 = r2.json()
    assert b2["status"] == "missed" and b2["referral"]["org"] and b2["message"]

    # out_of_scope 卡片（敏感拦截）
    r3 = client.post("/api/ask", json={"question": "我想起诉邻居怎么办"},
                     headers={"X-CSRF-Token": csrf})
    b3 = r3.json()
    assert b3["status"] == "out_of_scope" and b3["category"] == "涉法涉诉" and b3["advice"]


def test_ask_503_on_generation_failure(client, login, publish, run, monkeypatch, no_sleep):
    csrf = login()
    run(publish(Q, CONTENT))

    async def _bad(system_prompt, user_content):
        return {"answer": "x", "cited_ids": [99999], "sufficient": True}
    monkeypatch.setattr(llm, "chat", _bad)

    r = client.post("/api/ask", json={"question": Q}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 503
    assert r.json()["retryable"] is True                 # 前端提示可重试


def test_feedback_flow(client, login, publish, run):
    csrf = login()
    run(publish(Q, CONTENT))
    rid = client.post("/api/ask", json={"question": Q},
                      headers={"X-CSRF-Token": csrf}).json()["record_id"]
    rid2 = client.post("/api/ask", json={"question": "量子力学中的测不准原理是说什么的"},
                       headers={"X-CSRF-Token": csrf}).json()["record_id"]

    bad = client.post(f"/api/records/{rid}/feedback", json={"value": "good"},
                      headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400                        # 非法取值

    ok = client.post(f"/api/records/{rid}/feedback", json={"value": "useful"},
                     headers={"X-CSRF-Token": csrf})
    assert ok.json() == {"ok": True, "feedback": "useful"}

    conflict = client.post(f"/api/records/{rid2}/feedback", json={"value": "useful"},
                           headers={"X-CSRF-Token": csrf})
    assert conflict.status_code == 409                   # 仅已回答记录可评价


def test_feedback_rejects_other_users_record(client, login, publish, run):
    csrf_a = login("staff")
    run(publish(Q, CONTENT))
    rid = client.post("/api/ask", json={"question": Q},
                      headers={"X-CSRF-Token": csrf_a}).json()["record_id"]

    csrf_b = login("boss")                               # 同 client 换登录态
    r = client.post(f"/api/records/{rid}/feedback", json={"value": "useful"},
                    headers={"X-CSRF-Token": csrf_b})
    assert r.status_code == 403                          # 只能评价自己的记录


def test_login_lockout_after_max_fails(client):
    for _ in range(4):
        r = client.post("/api/auth/login", json={"username": "staff", "password": "wrong"})
        assert r.status_code == 401
    r5 = client.post("/api/auth/login", json={"username": "staff", "password": "wrong"})
    assert r5.status_code == 423                         # 第 5 次失败触发锁定
    r6 = client.post("/api/auth/login", json={"username": "staff", "password": PW})
    assert r6.status_code == 423                         # 锁定期内正确密码也拒绝


def test_change_password_rules_and_mcp_gate(client, login, db):
    csrf = login("rookie")                               # must_change_password=1
    r = client.post("/api/ask", json={"question": Q}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403                          # 首登未改密仅放行改密/登出

    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/auth/change-password",
                       json={"old_password": PW, "new_password": "1234567"},
                       headers=headers).status_code == 400          # 长度不足
    assert client.post("/api/auth/change-password",
                       json={"old_password": PW, "new_password": PW},
                       headers=headers).status_code == 400          # 与原密码相同
    assert client.post("/api/auth/change-password",
                       json={"old_password": "错误密码", "new_password": "NewPass4567"},
                       headers=headers).status_code == 403          # 原密码错误
    ok = client.post("/api/auth/change-password",
                     json={"old_password": PW, "new_password": "NewPass4567"},
                     headers=headers)
    assert ok.status_code == 200

    r2 = client.post("/api/ask", json={"question": Q}, headers=headers)
    assert r2.status_code == 200                         # 改密后解除拦截（空库 → missed）
    u = db.query(User).filter(User.username == "rookie").first()
    assert u.must_change_password == 0


def test_logout_invalidates_session(client, login):
    csrf = login()
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    r = client.post("/api/ask", json={"question": Q}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 401
