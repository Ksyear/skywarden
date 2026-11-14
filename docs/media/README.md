# Demo media

`skywarden_demo.mp4` — 65.7 s, 1600×1000, 30 fps, H.264 (yuv420p, faststart). A screen recording of the local console (WebGL map viewer), made 2026-11-15 by driving one browser tab over the DevTools protocol; loading pauses are cut. Nothing in it is sped up or edited beyond those cuts.

`skywarden_demo.gif` — 13 s, 960×600, 10 fps: corridor map following the camera (0:01.5–0:05.5), free orbit (0:08–0:12), overview jump and Measure (0:23.5–0:28.5).

| Time | Scene | Data |
|---|---|---|
| 0:00–0:08 | Video → 3D map: the map follows the camera while the video plays | corridor clip, model output on our SfM camera path (labelled as model output) |
| 0:08–0:20 | Paused: free orbit and zoom (1.76 M points, WebGL) | corridor |
| 0:20–0:24 | Overview map click jumps the replay | corridor |
| 0:24–0:29 | Measure: distance between two map points in map units; no metres because the map has no measured scale (RELATIVE) | corridor |
| 0:29–0:34 | Same corridor as SfM feature points (quality GOOD, 139/139 frames) | corridor |
| 0:34–0:51 | Metric scale: one length sets the scale, two other lengths check it → MEASURED_VERIFIED, distances in metres | synthetic room (SIMULATED) |
| 0:51–0:59 | Person tracking, chosen target LOCKED, MOCK yaw command (never sent) | TUM RGB-D fr3/walking_xyz, faces pixelated |
| 0:59–1:06 | Live run: detection, tracking, visual odometry and MOCK control at the clip's time base | corridor clip |

Notes
- In the synthetic scene the "tape" lengths were typed from the scene's ground-truth scale (0.22625 m per map unit, from the evaluation's Sim(3) alignment), so the two checks agree by construction; it shows the workflow, not an accuracy result. A real space needs tape-measured lengths.
- Faces in the TUM frames are pixelated in every recorded frame (head area of every detection and track, ±3 frames).
- All maps are relative scale unless stated, and NOT_NAVIGATION_SAFE. No vehicle was connected; all commands are MOCK.

Sources
- Indoor corridor: filmed by the repository author (REAL_VIDEO), used with their permission.
- TUM RGB-D dataset, sequence freiburg3_walking_xyz — Sturm et al., Technical University of Munich, https://cvg.cit.tum.de/data/datasets/rgbd-dataset, CC BY 4.0. Frames shown with faces pixelated.
- Synthetic room: rendered by this repository (`packages/sim/synthetic_scene.py`).
