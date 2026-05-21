"""Unit tests for Task CRUD."""

from app.db.crud import task_crud
from app.db.models.task import TaskStatus


async def test_create_task(session, user):
    task = await task_crud.create(
        session,
        user_id=user.id,
        title="Morning run",
        duration_mins=30,
        is_fixed=True,
        status=TaskStatus.PENDING.value,
    )
    await session.commit()

    assert task.id is not None
    assert task.user_id == user.id
    assert task.status == TaskStatus.PENDING.value
    assert task.is_fixed is True
    assert task.requires_proof is False


async def test_get_and_update_task(session, task):
    updated = await task_crud.update(
        session,
        task,
        status=TaskStatus.PUSHED.value,
        requires_proof=True,
    )
    await session.commit()

    fetched = await task_crud.get(session, updated.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.PUSHED.value
    assert fetched.requires_proof is True


async def test_list_tasks(session, user):
    await task_crud.create(
        session,
        user_id=user.id,
        title="Task A",
        duration_mins=15,
    )
    await task_crud.create(
        session,
        user_id=user.id,
        title="Task B",
        duration_mins=45,
    )
    await session.commit()

    tasks = await task_crud.list(session)
    assert len(tasks) == 2


async def test_delete_task(session, task):
    await session.commit()
    await task_crud.delete(session, task)
    await session.commit()
    assert await task_crud.get(session, task.id) is None
