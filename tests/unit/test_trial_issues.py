import mongomock
import pytest
from bson import ObjectId
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


def _legacy_issue(db, **values):
    doc = {"session_id": "s", "query": "烫金机有蓝牙吗", "key": "answer-1",
           "issue_type": "missing_material", "status": "open", "created_at": 1,
           "owner": "", "resolution": "", "completed_at": None, "item_names": ["HAK180"]}
    doc.update(values)
    return db[settings.mongo.query_issues_collection].insert_one(doc).inserted_id


def test_query_retries_and_gap_scan_form_one_work_item_without_deleting_sources(db):
    for index, kind in enumerate(["missing_material", "reading_failure", "missing_material"]):
        _legacy_issue(db, key=str(index), issue_type=kind, created_at=index + 1)
    rows = issues.list_issues()
    assert len(rows) == 1
    assert rows[0]["record_count"] == 3
    assert set(rows[0]["issue_types"]) == {"missing_material", "reading_failure"}
    assert len(rows[0]["related_records"]) == 3
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 3


def test_legacy_product_context_is_recovered_from_its_own_answer_and_gap(db):
    answer_id, gap_id = ObjectId(), ObjectId()
    db[settings.mongo.chat_message_collection].insert_one({
        "_id": answer_id, "session_id": "s", "role": "assistant", "item_names": ["HAK 180"],
    })
    db[settings.mongo.k_gaps_collection].insert_one({"_id": gap_id, "session_id": "s", "item_names": ["HAK 180"]})
    _legacy_issue(db, answer_id=str(answer_id), business_tag="product_knowledge", item_names=None)
    _legacy_issue(db, gap_id=str(gap_id), issue_type="reading_failure", business_tag="", item_names=None)
    rows = issues.list_issues()
    assert len(rows) == 1 and rows[0]["item_names"] == ["HAK 180"]
    assert rows[0]["record_count"] == 2


@pytest.mark.parametrize("different", [
    {"session_id": "other"}, {"item_names": ["HL3040"]}, {"business_tag": "after_sales"},
])
def test_work_items_do_not_merge_across_session_product_or_business(db, different):
    _legacy_issue(db, item_names=["HAK180"], business_tag="product_knowledge")
    _legacy_issue(db, **{"item_names": ["HAK180"], "business_tag": "product_knowledge", **different})
    rows = issues.list_issues()
    assert len(rows) == 2
    assert all(row["record_count"] == 1 for row in rows)


def test_question_formatting_merges_but_changed_meaning_does_not(db):
    _legacy_issue(db, query=" 烫金机有蓝牙吗？ ", item_names=["HAK 180"])
    _legacy_issue(db, query="烫金机有蓝牙吗", item_names=["HAK180"])
    _legacy_issue(db, query="烫金机没有蓝牙吗", item_names=["HAK180"])
    assert sorted(row["record_count"] for row in issues.list_issues()) == [1, 2]


def test_grouping_happens_before_display_limit(db):
    other = _legacy_issue(db, query="另一个问题", created_at=0)
    for index in range(110):
        _legacy_issue(db, key=str(index), created_at=index + 1)
    rows = issues.list_issues(limit=2)
    assert len(rows) == 2
    assert str(other) in {row["id"] for row in rows}


def test_save_group_updates_members_and_keeps_previous_handling_record(db):
    first = _legacy_issue(db, owner="员工A", status="in_progress", resolution="已检查资料")
    second = _legacy_issue(db, issue_type="reading_failure", created_at=2)
    ok, message = issues.update_issue(str(first), owner="员工B", status="resolved", resolution="已修复检索并验证")
    assert ok, message
    members = list(db[settings.mongo.query_issues_collection].find())
    assert all(row["status"] == "resolved" and row["owner"] == "员工B" for row in members)
    assert all(row["completed_at"] for row in members)
    original = next(row for row in members if row["_id"] == first)
    assert original["handling_history"][-1]["resolution"] == "已检查资料"
    assert issues.list_issues(status="unresolved") == []
    assert len(issues.list_issues(status="resolved")) == 1
    assert db[settings.mongo.query_issues_collection].find_one({"_id": second})


def test_resolved_record_is_kept_separate_from_a_new_failure(db):
    previous = _legacy_issue(db, status="resolved", owner="员工A", resolution="已处理", completed_at=2)
    new = _legacy_issue(db, key="answer-2", created_at=3)
    rows = issues.list_issues(status="unresolved")
    assert len(rows) == 1 and rows[0]["id"] == str(new)
    assert issues.update_issue(str(new), owner="员工B", status="in_progress", resolution="排查中")[0]
    assert db[settings.mongo.query_issues_collection].find_one({"_id": previous})["resolution"] == "已处理"


def test_unbound_gap_registration_does_not_enter_group(db):
    _legacy_issue(db)
    _legacy_issue(db, issue_type="reading_failure", registration_status="staged")
    rows = issues.list_issues()
    assert len(rows) == 1 and rows[0]["record_count"] == 1
    assert rows[0]["issue_types"] == ["missing_material"]


def test_new_issue_stores_product_and_business_context(db):
    issues.open_issue({"session_id": "s", "answer_id": "a", "original_query": "支持蓝牙吗",
                       "item_names": ["HAK180"], "business_tag": "product_knowledge"})
    stored = db[settings.mongo.query_issues_collection].find_one()
    assert stored["item_names"] == ["HAK180"]
    assert stored["business_tag"] == "product_knowledge"


def test_foreign_session_reference_cannot_supply_product_context(db):
    answer_id = db[settings.mongo.chat_message_collection].insert_one({
        "session_id": "other-session", "role": "assistant", "item_names": ["HAK180"],
    }).inserted_id
    _legacy_issue(db, answer_id=str(answer_id), item_names=None)
    _legacy_issue(db, item_names=["HAK180"])
    rows = issues.list_issues()
    assert len(rows) == 2
    assert any(row["item_names"] == [] for row in rows)


def test_english_word_boundaries_are_preserved_when_grouping(db):
    _legacy_issue(db, query="can a b connect?")
    _legacy_issue(db, query="can ab connect?")
    assert len(issues.list_issues()) == 2


def test_unknown_product_records_are_not_automatically_merged(db):
    _legacy_issue(db, item_names=[])
    _legacy_issue(db, item_names=[])
    assert len(issues.list_issues()) == 2


@pytest.mark.parametrize("status", ["in_progress", "resolved"])
def test_stale_card_cannot_overwrite_completed_work(db, status):
    oid = _legacy_issue(db, status="resolved", owner="员工B", resolution="已完成验证", completed_at=10)
    ok, message = issues.update_issue(str(oid), owner="员工A", status=status, resolution="旧卡片保存")
    assert not ok and "完成" in message
    stored = db[settings.mongo.query_issues_collection].find_one({"_id": oid})
    assert stored["status"] == "resolved" and stored["owner"] == "员工B"
    assert stored["resolution"] == "已完成验证" and stored["completed_at"] == 10
