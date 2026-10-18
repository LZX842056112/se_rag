import mongomock
import pytest
from app.evolution import issues
from app.shared.config import settings

@pytest.fixture
def db(monkeypatch):
    database=mongomock.MongoClient().trial
    monkeypatch.setattr(issues,"get_collection",lambda name:database[name])
    return database

def test_issue_requires_owner_and_resolution_before_completion(db):
    issue_id=issues.open_issue({"session_id":"s","turn_id":"t","answer_id":"a","original_query":"价格", "issue_type":"missing_material"})
    assert issue_id
    assert issues.update_issue(issue_id, owner="", status="resolved", resolution="done")[0] is False
    assert issues.update_issue(issue_id, owner="员工A", status="in_progress", resolution="")[0]
    assert issues.update_issue(issue_id, owner="员工A", status="resolved", resolution="已补充政策")[0]
    doc=db[settings.mongo.query_issues_collection].find_one()
    assert doc["completed_at"] >= doc["created_at"]
    assert doc["owner"] == "员工A"

def test_issue_creation_is_idempotent_per_answer(db):
    state={"session_id":"s","turn_id":"t","answer_id":"a","original_query":"q", "issue_type":"missing_material"}
    assert issues.open_issue(state) == issues.open_issue(state)
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 1
