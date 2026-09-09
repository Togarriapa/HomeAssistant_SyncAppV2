"""Compose setup controls and the single state-owning operation worker."""

import time
from pathlib import Path
from queue import Empty
from typing import Any

from .config import Config
from .control import Control
from .engine import Engine
from .errors import Failure
from .git import GitRepository
from .homeassistant import HomeAssistant
from .http import GitHubGuard
from .journal import Journal
from .keys import KeyManager, SSHIdentity


class Application:
    def __init__(
        self,
        config: Config,
        directory: Path,
        config_root: Path,
        journal: Journal,
        homeassistant: HomeAssistant,
    ) -> None:
        self.config = config
        self.directory = directory
        self.journal = journal
        self.keys = KeyManager(directory / "keys", journal)
        self.control = Control()
        self.last_action: dict[str, Any] | None = None
        self.engine = Engine(
            directory,
            config_root,
            journal,
            lambda: self.repository(),
            homeassistant,
            sync_interval=config.sync_interval_seconds,
            retrigger_interval=config.retrigger_interval_seconds,
        )
        self.recovered_jobs = journal.recover()
        self.publish()

    def repository(self, identity: SSHIdentity | None = None) -> GitRepository:
        if not self.config.repository_target or not self.config.metadata_token:
            raise Failure("repository_setup_incomplete")
        guard = GitHubGuard(self.config.repository_target, self.config.metadata_token, self.journal)
        return GitRepository(
            self.directory / "repository.git",
            guard.remote,
            identity=identity or self.keys.identity(),
            guard=guard.verify,
        )

    def publish(self, *, busy: bool = False) -> None:
        self.control.publish(
            {
                **self.engine.status(),
                "keys": self.keys.status(),
                "repository": self.config.repository_target,
                "configured": bool(self.config.repository_target and self.config.metadata_token),
                "busy": busy,
                "action": self.last_action,
                "recovered_jobs": self.recovered_jobs,
            }
        )

    def _action(self, action: dict[str, str]) -> None:
        name = action["action"]
        if name in ("generate", "refresh"):
            self.keys.generate()
        elif name == "test":
            self.keys.test(
                self.config.repository_target,
                lambda key: self.repository(key).test_access(key.private_key.name),
            )
        elif name == "activate":
            if any(
                j.phase in ("applying", "checking", "observing", "promoting", "restoring")
                and j.status != "succeeded"
                for j in self.journal.jobs()
            ):
                raise Failure("recovery_must_finish_before_key_activation")
            self.keys.activate(self.config.repository_target)
        elif name == "initialize":
            self.repository()  # Require a configured repository and active key before enqueuing.
            self.engine.request_initialize()
        elif name == "retry":
            self.journal.retry(action.get("job", ""))
        else:
            raise Failure("unsupported_action")

    def step(self) -> None:
        try:
            action = self.control.actions.get_nowait()
        except Empty:
            action = None
        if action:
            self.last_action = {"id": action["id"], "name": action["action"], "status": "running"}
            self.publish(busy=True)
            try:
                self._action(action)
                self.last_action["status"] = "completed"
            except Failure as error:
                self.last_action.update(status="failed", error=error.code)
            except (OSError, ValueError, KeyError, TypeError):
                self.last_action.update(status="failed", error="action_state_invalid")
        self.publish(busy=True)
        self.engine.tick(time.time())
        self.publish()
