"""Unit tests for User CRUD."""

from app.db.crud import user_crud


async def test_create_user(session):
    user = await user_crud.create(
        session,
        telegram_chat_id=987654321,
        timezone="UTC",
        max_debt_limit=10.0,
    )
    await session.commit()

    assert user.id is not None
    assert user.telegram_chat_id == 987654321
    assert user.onboarding_status == "incomplete"
    assert user.onboarding_step == "welcome"
    assert user.onboarding_data == {}


async def test_get_user(session, user):
    await session.commit()
    fetched = await user_crud.get(session, user.id)
    assert fetched is not None
    assert fetched.id == user.id


async def test_update_user(session, user):
    updated = await user_crud.update(
        session,
        user,
        onboarding_status="complete",
        onboarding_step="done",
        onboarding_data={"goals": ["ship project"]},
    )
    await session.commit()

    assert updated.onboarding_status == "complete"
    assert updated.onboarding_data == {"goals": ["ship project"]}


async def test_list_and_delete_user(session):
    user = await user_crud.create(session, telegram_chat_id=111222333)
    await session.commit()

    users = await user_crud.list(session)
    assert len(users) == 1

    await user_crud.delete(session, user)
    await session.commit()

    assert await user_crud.get(session, user.id) is None
