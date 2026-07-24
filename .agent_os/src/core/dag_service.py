"""DagService — DAG 编排 + git 回退的公共逻辑。

从 AgentOS 抽取 dag_checkout / dag_status / _find_workspace_for_run。
"""
import json
import logging
import os
import subprocess

from . import dag_planner as dp

logger = logging.getLogger("agent_os")


class DagService:
    """DAG 编排服务：状态查询、步骤回退、workspace 定位。"""

    def __init__(self, recorder, project_root: str, runs: dict):
        self._recorder = recorder
        self._project_root = project_root
        self._runs = runs  # AgentOS._registry.runs 的引用

    # ---- workspace 定位 ----

    def find_workspace_for_run(self, run_id: str) -> str | None:
        """通过 state/runs.json 查找 run 的 workspace_path。"""
        state_file = os.path.join(self._project_root, "state", "runs.json")
        if not os.path.exists(state_file):
            return None
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            for run in state.get("runs", []):
                if run.get("run_id") == run_id:
                    return run.get("workspace_path")
        except Exception:
            pass
        return None

    # ---- DAG 状态查询 ----

    def dag_status(self, run_id: str) -> dict:
        """返回该 run 所在 workspace 的 DAG 状态。"""
        ri = self._runs.get(run_id)
        ws = ri.workspace_path if ri and ri.workspace_path else self.find_workspace_for_run(run_id)
        if not ws:
            return {"ok": False, "error": "run not found or no workspace"}
        return self._dag_status_for_ws(ws)

    def dag_status_by_workspace(self, workspace_id: str) -> dict:
        """通过 workspace 目录名直接查 DAG 状态。"""
        ws = os.path.join(self._project_root, ".agent_os", "workspaces", workspace_id)
        if not os.path.isdir(ws):
            return {"ok": False, "error": "workspace not found"}
        return self._dag_status_for_ws(ws)

    def _resolve_workspace(self, run_id: str) -> str | None:
        ri = self._runs.get(run_id)
        if ri and ri.workspace_path:
            return ri.workspace_path
        return self.find_workspace_for_run(run_id)

    def _dag_status_for_ws(self, ws: str) -> dict:
        """给定 workspace 路径，返回 DAG 状态（含 commit + agent 产出摘要）。"""
        try:
            dag = dp.load_dag(ws)
            steps = dag.get("steps", [])
            order = dp.topo_order(steps) if steps else []
            by_id = {s["id"]: s for s in steps}
            step_commits = self._recorder.list_step_commits(ws) if self._recorder else []
            ordered = []
            for i in order:
                step = by_id[i]
                node = {
                    "id": step["id"],
                    "name": step.get("name", ""),
                    "status": step.get("status", "pending"),
                    "depends_on": step.get("depends_on", []),
                    "prompt": step.get("prompt", "")[:200],
                    "summary": "",
                    "files": [],
                    "sha": None,
                    "date": None,
                }
                for c in step_commits:
                    if c.get("step_id") == step["id"]:
                        node["sha"] = c.get("sha")
                        node["date"] = c.get("date")
                        node["files"] = self._recorder.commit_files(c.get("sha"), ws) if self._recorder else []
                        msg = c.get("message", "")
                        if "] " in msg:
                            node["summary"] = msg.split("] ", 1)[1][:200]
                        break
                for run in self._runs.values():
                    if run.step_id == step["id"] and run.reported_result:
                        node["summary"] = run.reported_result[:200]
                        break
                ordered.append(node)
        except Exception as e:
            return {"ok": False, "error": f"load dag failed: {e}"}
        return {"ok": True, "steps": ordered, "step_commits": step_commits}

    # ---- DAG 回退 (checkout) ----

    def dag_checkout(self, run_id: str, step_id: str,
                     rerun_downstream: bool = False) -> dict:
        """回退 workspace 到某 step 完成时的快照，重置该 step + 下游 pending。"""
        if not self._recorder:
            return {"ok": False, "error": "git disabled"}
        ws = self._resolve_workspace(run_id)
        if not ws:
            return {"ok": False, "error": "run has no workspace"}

        # 1) git checkout
        co = self._recorder.checkout_step(step_id, ws)
        if not co.get("ok"):
            return {"ok": False, "error": co.get("error", "checkout failed"),
                    "sha": co.get("sha")}

        # 2) dag.json 重置
        affected: list[str] = []
        try:
            dag = dp.load_dag(ws)
            steps = dag.get("steps", [])
            affected = dp.get_descendants(steps, step_id)
            dp.reset_steps(steps, affected)
            dp.save_dag(ws, dag)
        except Exception as e:
            logger.warning(f"dag_checkout: reset dag.json failed: {e}")

        # 3) commit 重置后的 dag.json
        if affected:
            self._commit_reset_dag(ws, step_id, len(affected))

        logger.info(f"dag_checkout: run={run_id[:8]} step={step_id} "
                    f"sha={co.get('sha', '')[:8]} affected={affected}")
        return {"ok": True, "sha": co.get("sha"), "affected_steps": affected,
                "restored": co.get("restored", []), "removed": co.get("removed", []),
                "rerun_downstream": rerun_downstream}

    def _commit_reset_dag(self, ws: str, step_id: str, affected_count: int) -> None:
        try:
            git_cwd = self._recorder._git_cwd(ws)
            subprocess.run(["git", "add", "."], cwd=git_cwd,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
            r = subprocess.run(
                ["git", "commit", "-m",
                 f"[checkout:{self._recorder._ws_id(ws)}:{step_id}] reset {affected_count} step(s) to pending"],
                cwd=git_cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15)
            if r.returncode != 0:
                stderr = r.stderr.strip()
                if "nothing to commit" not in stderr and "no changes" not in stderr:
                    logger.warning(f"dag_checkout: commit reset failed: {stderr[:200]}")
        except Exception as e:
            logger.warning(f"dag_checkout: commit exception: {e}")
