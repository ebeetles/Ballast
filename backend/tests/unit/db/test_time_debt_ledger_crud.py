"""Unit tests for append-only TimeDebtLedger CRUD."""

from app.db.crud import ledger_crud


async def test_create_ledger_with_task(session, ledger_entry):
    await session.commit()
    assert ledger_entry.hours_added == 1.5
    assert ledger_entry.reason == "Task pushed"
    assert ledger_entry.task_id is not None


async def test_create_ledger_without_task(session, user):
    entry = await ledger_crud.create(
        session,
        user_id=user.id,
        task_id=None,
        hours_added=-0.5,
        reason="Manual adjustment",
    )
    await session.commit()

    fetched = await ledger_crud.get(session, entry.id)
    assert fetched is not None
    assert fetched.task_id is None
    assert fetched.hours_added == -0.5


async def test_ledger_get_and_list(session, ledger_entry):
    await session.commit()
    fetched = await ledger_crud.get(session, ledger_entry.id)
    assert fetched is not None

    entries = await ledger_crud.list(session)
    assert len(entries) == 1


def test_ledger_crud_is_append_only():
    assert not hasattr(ledger_crud, "update")
    assert not hasattr(ledger_crud, "delete")
