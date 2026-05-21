"""Unit tests for UserProfileInsight CRUD."""

from app.db.crud import insight_crud


async def test_create_insight(session, insight):
    await session.commit()
    assert insight.category == "focus"
    assert insight.strength == 8


async def test_get_insight(session, insight):
    await session.commit()
    fetched = await insight_crud.get(session, insight.id)
    assert fetched is not None
    assert fetched.insight == "Works best in morning blocks"


async def test_update_insight(session, insight):
    updated = await insight_crud.update(
        session,
        insight,
        insight="Prefers 90-minute deep work blocks",
        strength=9,
    )
    await session.commit()

    assert updated.insight == "Prefers 90-minute deep work blocks"
    assert updated.strength == 9


async def test_list_and_delete_insight(session, user):
    row = await insight_crud.create(
        session,
        user_id=user.id,
        category="energy",
        insight="Afternoon slump",
        strength=6,
    )
    await session.commit()

    rows = await insight_crud.list(session)
    assert len(rows) == 1

    await insight_crud.delete(session, row)
    await session.commit()
    assert await insight_crud.get(session, row.id) is None
