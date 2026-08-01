"""LaunchMixin — 进程启动 3 阶段管道 + resume（从 agent_os.py 拆出）。"""
import logging
import os
import threading
import uuid

from .env_config import build_agent_env
from .session.prompt import PromptBuilder
from .models import RunStatus, RunInfo
from .agents import Agent

logger = logging.getLogger("agent_os")


class LaunchMixin:
    """进程启动管道：prepare -> launch -> finalize + resume。"""

    def _prepare_launch(self, prompt, parent_run_id, interactive, env_extras,
                         system_prompt, model, workspace_name, goal):
        prompt = self._sanitize_unicode(prompt)
        if system_prompt:
            system_prompt = self._sanitize_unicode(system_prompt)
        run_id = uuid.uuid4().hex[:10]
        session_id = str(uuid.uuid4())
        if model is None:
            model = self.default_model
        logger.info(f"start_run: run_id={run_id}, session_id={session_id[:13]}, "
                    f"prompt={prompt[:50]}, interactive={interactive}, model={model}, goal={goal}")
        env = build_agent_env(run_id, self.project_root, self.port, workspace_name, env_extras)
        if env_extras:
            env.update(env_extras)
        if not parent_run_id and not system_prompt:
            system_prompt = PromptBuilder.build_root_system_prompt(
                env.get("AGENT_OS_WORKSPACE", ".agent_os/workspaces/<run>/"))
        return {"run_id": run_id, "session_id": session_id, "prompt": prompt,
                "system_prompt": system_prompt, "model": model, "env": env,
                "interactive": interactive}

    def _launch_process(self, config):
        logger.info(f"[{config['run_id'][:8]}] Launching agent...")
        try:
            return self._backend.launch(
                prompt=config["prompt"], model=config["model"],
                session_id=config["session_id"], resume_session=None,
                system_prompt=config["system_prompt"],
                cwd=self.project_root, env=config["env"])
        except Exception as e:
            logger.error(f"[{config['run_id'][:8]}] Launch failed: {e}")
            run_info = RunInfo(run_id=config["run_id"], prompt=config["prompt"], status=RunStatus.FAILED)
            run_info.add_event("error", text=f"[ERROR] Popen failed: {e}")
            self._registry.runs[config["run_id"]] = run_info
            self._agents[config["run_id"]] = Agent.for_run(run_info, self)
            self._mark_dirty()
            return {"error": str(e), "run_id": config["run_id"]}

    def _finalize_start(self, config, handle, parent_run_id, task_type, goal, supervisor):
        run_id = config["run_id"]
        _depth = 0
        parent = self._get_parent(parent_run_id)
        if parent:
            _depth = (getattr(parent, '_depth', 0) or 0) + 1

        run_info = RunInfo(
            run_id=run_id, prompt=config["prompt"],
            session_id=config["session_id"], parent_run_id=parent_run_id,
            interactive=config["interactive"], model=config["model"],
            task_type=task_type,
            workspace_path=config["env"].get("AGENT_OS_WORKSPACE"),
            step_id=config["env"].get("AGENT_OS_STEP_ID"),
            system_prompt=config["system_prompt"],
            goal=goal, supervisor=supervisor,
        )
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())
        run_info._depth = _depth
        run_info.turn_markers.append((0, config["prompt"]))
        self._registry.runs[run_id] = run_info
        agent = Agent.for_run(run_info, self)
        self._agents[run_id] = agent
        agent.start()
        run_info.add_event("turn", index=1)
        run_info.add_event("prompt", text=config["prompt"], role="user", source="user")

        if parent:
            parent.children_run_ids.append(run_id)

        marker_path = os.path.join(self.project_root, ".agent_os_run_id")
        try:
            with open(marker_path, "w") as f:
                f.write(run_id)
        except Exception:
            pass

        self._start_reader(run_id)
        self._mark_dirty()
        return run_id

    def _launch_resume(self, run_info, prompt, model):
        """resume 会话基础设施层（backend.launch + reader）。"""
        env = os.environ.copy()
        env.update({
            "AGENT_OS_RUN_ID": run_info.run_id,
            "AGENT_OS_PORT": str(self.port),
        })
        if run_info.workspace_path:
            env["AGENT_OS_WORKSPACE"] = run_info.workspace_path
            workspace_cwd = self.project_root
        else:
            env = build_agent_env(run_info.run_id, self.project_root, self.port)
            workspace_cwd = self.project_root

        handle = self._backend.launch(
            prompt=prompt, model=model, session_id=None,
            resume_session=run_info.session_id,
            system_prompt=run_info.system_prompt,
            cwd=workspace_cwd, env=env,
        )

        self._transition(run_info, RunStatus.RUNNING)
        run_info.completed_at = None
        run_info.exit_code = None
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())

        self._start_reader(run_info.run_id)
        self._mark_dirty()
        return True
