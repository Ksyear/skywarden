"""Labels that must travel with every map so the UI never mixes real, relative and simulated data."""
from enum import Enum
from pathlib import Path


class MapSource(str, Enum):
    STORED_RECONSTRUCTION = "STORED_RECONSTRUCTION"  # loaded from disk after a finished build
    CURRENT_SESSION = "CURRENT_SESSION"  # being built right now
    SIMULATED = "SIMULATED"  # ground truth given by a generator, not a measurement


class DataOrigin(str, Enum):
    REAL_VIDEO = "REAL_VIDEO"  # user's own capture
    PUBLIC_DATASET = "PUBLIC_DATASET"  # real capture from a licensed public dataset
    SYNTHETIC = "SYNTHETIC"  # rendered by packages/sim
    UNSPECIFIED = "UNSPECIFIED"


class Geometry(str, Enum):
    SPARSE_SFM = "SPARSE_SFM"


class Scale(str, Enum):
    RELATIVE = "RELATIVE"  # monocular SfM: unknown metric scale
    METRIC_ANCHORED = "METRIC_ANCHORED"


class CalibrationStatus(str, Enum):
    PROVIDED = "PROVIDED"  # intrinsics given by the user / dataset, kept fixed
    UNCALIBRATED_ESTIMATED = "UNCALIBRATED_ESTIMATED"  # estimated by COLMAP from the images


NAV_SAFETY = "NOT_NAVIGATION_SAFE"
SYNTHETIC_MARKER = "SYNTHETIC_DATA"  # file written next to generated frames by packages/sim


def is_synthetic_input(path: str | Path) -> bool:
    d = Path(path) if Path(path).is_dir() else Path(path).parent
    return (d / SYNTHETIC_MARKER).exists() or (d.parent / SYNTHETIC_MARKER).exists()


def map_source_for(origin: DataOrigin) -> MapSource:
    """A finished build from a synthetic render stays SIMULATED; real builds become stored reconstructions."""
    return MapSource.SIMULATED if origin == DataOrigin.SYNTHETIC else MapSource.STORED_RECONSTRUCTION
