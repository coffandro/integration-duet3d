"""Task utilities for the Duet SimplyPrint connector."""

import asyncio


def async_task(func):
    """Run a function as a task."""

    async def wrapper(*args, **kwargs):

        async def _inner(*args, **kwargs):
            try:
                await func(*args, **kwargs)
            except Exception as e:
                args[0].logger.exception(
                    "An exception occurred while running an async function",
                    exc_info=e,
                )
                # TODO: log to sentry

        task = args[0].event_loop.create_task(_inner(*args, **kwargs))
        args[0]._background_task.add(task)
        task.add_done_callback(args[0]._background_task.discard)
        return task

    return wrapper


def async_supress(func):
    """Suppress exceptions in an async function."""

    async def wrapper(*args, **kwargs):
        try:
            await func(*args, **kwargs)
        except asyncio.CancelledError as e:
            await args[0].duet.close()
            raise e
        except Exception as e:
            args[0].logger.exception(
                "An exception occurred while running an async function",
                exc_info=e,
            )

    return wrapper
