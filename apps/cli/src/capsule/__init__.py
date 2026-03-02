"""Capsule isolation for Glock sessions.

Capsules provide isolated execution environments for tools,
either via process isolation or Docker containers.
"""

from .factory import CapsuleFactory, CapsuleMode
from .process import ProcessCapsule
from .docker import DockerCapsule
from .policy import SandboxPolicy, SandboxMode, load_policy_from_config
from .manager import CapsuleManager

__all__ = [
    "CapsuleFactory",
    "CapsuleMode",
    "ProcessCapsule",
    "DockerCapsule",
    "SandboxPolicy",
    "SandboxMode",
    "CapsuleManager",
    "load_policy_from_config",
]
