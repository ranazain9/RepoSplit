"""Agent registry - the ordered execution DAG the Supervisor walks.

To add an agent: implement `BaseAgent`, give it a `phase`, `requires` and `produces`, and insert
it here. The Supervisor validates `requires`/`produces` at runtime, so ordering mistakes fail fast.
"""

from __future__ import annotations

from reposplit.core.base_agent import AgentContext, BaseAgent


def default_pipeline(ctx: AgentContext) -> list[BaseAgent]:
    from reposplit.agents.architect.agent import ArchitectAgent
    from reposplit.agents.contract.agent import ContractAgent
    from reposplit.agents.data.agent import DataAgent
    from reposplit.agents.governance.agent import GovernanceAgent
    from reposplit.agents.parity.agent import ParityAgent
    from reposplit.agents.strangler.agent import StranglerAgent
    from reposplit.generators.scaffold_agent import ScaffoldAgent

    return [
        ArchitectAgent(ctx),
        DataAgent(ctx),
        ContractAgent(ctx),
        StranglerAgent(ctx),
        ScaffoldAgent(ctx),
        ParityAgent(ctx),
        GovernanceAgent(ctx),
    ]


def scaffold_agent(ctx: AgentContext) -> BaseAgent:
    from reposplit.generators.scaffold_agent import ScaffoldAgent

    return ScaffoldAgent(ctx)


def describe_agents() -> list[dict[str, str]]:
    """Static description for `reposplit agents` and the docs."""
    from reposplit.agents.architect.agent import ArchitectAgent
    from reposplit.agents.contract.agent import ContractAgent
    from reposplit.agents.data.agent import DataAgent
    from reposplit.agents.governance.agent import GovernanceAgent
    from reposplit.agents.parity.agent import ParityAgent
    from reposplit.agents.strangler.agent import StranglerAgent
    from reposplit.generators.scaffold_agent import ScaffoldAgent

    rows = []
    for cls in (ArchitectAgent, DataAgent, ContractAgent, StranglerAgent, ScaffoldAgent, ParityAgent, GovernanceAgent):
        rows.append(
            {
                "name": cls.name,
                "phase": cls.phase.value,
                "llm": "yes" if cls.uses_llm else "deterministic",
                "requires": ", ".join(cls.requires),
                "produces": ", ".join(cls.produces),
                "description": cls.description,
            }
        )
    return rows
