from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from src.outreach.api import app as outreach_app


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with outreach_app.router.lifespan_context(outreach_app):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/outreach", outreach_app)