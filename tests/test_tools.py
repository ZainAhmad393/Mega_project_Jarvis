import os
import pytest
import tools
from memory import MemoryManager

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_jarvis.db")


@pytest.fixture(autouse=True)
def setup_teardown_db():
    """Use a clean test database for each test."""
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except Exception:
            pass
    # Reset singleton to test db
    test_mem = MemoryManager.reset_instance(db_path=TEST_DB)
    tools.memory = test_mem
    yield
    test_mem.close()
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except Exception:
            pass


def test_todo_lifecycle():
    # 1. Add todo
    res1 = tools.add_todo("Complete quarterly report")
    assert res1["status"] == "success"
    assert "Complete quarterly report" in res1["message"]

    # 2. Get todos
    res2 = tools.get_todo()
    assert res2["status"] == "success"
    assert len(res2["todos"]) == 1
    assert res2["todos"][0]["task"] == "Complete quarterly report"

    # 3. Daily plan
    plan = tools.daily_plan()
    assert plan["status"] == "plan_ready"
    assert "Complete quarterly report" in plan["message"]

    # 4. Complete todo
    todo_id = res1["todo_id"]
    res3 = tools.complete_todo(todo_id)
    assert res3["status"] == "success"
    assert "completed" in res3["message"].lower()

    # 5. Check no pending left
    res4 = tools.get_todo()
    assert res4["status"] == "empty"
    assert len(res4["todos"]) == 0


def test_remember_and_recall():
    res1 = tools.remember("boss_email", "boss@company.com")
    assert res1["status"] == "remembered"
    assert "boss@company.com" in res1["message"]

    res2 = tools.remember("favorite_food", "Biryani")
    assert res2["status"] == "remembered"

    recall_all = tools.recall()
    assert recall_all["status"] == "recalled"
    keys = {m["key"]: m["value"] for m in recall_all["data"]}
    assert keys["boss_email"] == "boss@company.com"
    assert keys["favorite_food"] == "Biryani"

    recall_single = tools.recall("boss_email")
    assert recall_single["status"] == "recalled"
    assert recall_single["data"]["value"] == "boss@company.com"


def test_safety_pending_email():
    # Call send_email - MUST create pending action, NEVER execute immediately
    resp = tools.send_email(to="client@example.com", subject="Project Update", body="The project is ready.")
    assert resp["status"] == "pending_confirmation"
    action_id = resp["action_id"]

    # Verify action stored in DB
    pending = tools.memory.get_latest_pending_action()
    assert pending is not None
    assert pending["id"] == action_id
    assert pending["action_type"] == "email"
    assert pending["payload"]["to"] == "client@example.com"

    # Confirm action (handles unconfigured credentials safely)
    confirm_resp = tools.execute_pending_action(action_id, confirm=True)
    assert confirm_resp["status"] in ("sent", "config_missing", "error")

    # Verify pending cleared
    assert tools.memory.get_latest_pending_action() is None


def test_safety_cancel_whatsapp():
    # Call send_whatsapp - pending action created
    resp = tools.send_whatsapp(contact_or_phone="+92 3134330393", message="Hello there!")
    assert resp["status"] == "pending_confirmation"
    action_id = resp["action_id"]

    # User cancels
    cancel_resp = tools.execute_pending_action(action_id, confirm=False)
    assert cancel_resp["status"] == "cancelled"

    # Verify pending cleared
    assert tools.memory.get_latest_pending_action() is None


def test_play_music():
    resp = tools.play_music("stealth")
    assert resp["status"] == "success"
    assert "stealth" in resp["song"]
    assert "youtube.com" in resp["url"]

    # Test unknown song fallback
    resp_unknown = tools.play_music("random_synthwave_xyz")
    assert resp_unknown["status"] == "search"
    assert "youtube.com" in resp_unknown["url"]


def test_client_confirmation_dispatch():
    import asyncio
    from client import jarvis_ai, ai_process
    jarvis_ai.reset()

    # Create pending action
    tools.send_email(to="partner@enterprise.com", subject="Review", body="Check draft.")
    assert tools.memory.get_latest_pending_action() is not None

    # Confirm via natural speech
    reply, sentiment = asyncio.run(ai_process("haan send kar do"))
    assert tools.memory.get_latest_pending_action() is None
    assert "Sir" in reply


def test_call_intents():
    from client import jarvis_ai
    jarvis_ai.reset()

    # Test Video Call Trigger
    res_video = jarvis_ai.process("Jarvis start video call")
    assert res_video["action"] == "start_video_call"
    assert "video call activated" in res_video["response"].lower()

    # Test Audio Call Trigger
    res_audio = jarvis_ai.process("Jarvis audio call")
    assert res_audio["action"] == "start_audio_call"
    assert "audio call connected" in res_audio["response"].lower()

    # Test Call End Trigger ("ok bye")
    res_bye = jarvis_ai.process("ok bye")
    assert res_bye["action"] == "end_call"
    assert "call ended" in res_bye["response"].lower()
