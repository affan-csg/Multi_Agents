from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from src.csg.api import app as research_app


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with research_app.router.lifespan_context(research_app):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/research", research_app)