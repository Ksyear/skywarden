# Source record: TUM RGB-D fr1/desk
- URL: https://cvg.cit.tum.de/rgbd/dataset/freiburg1/rgbd_dataset_freiburg1_desk.tgz
- Dataset page: https://cvg.cit.tum.de/data/datasets/rgbd-dataset
- License: Creative Commons 4.0 Attribution (CC BY 4.0), as stated on the dataset page (checked 2026-11-08)
- Archive size: 344011403 bytes; SHA-256 e983d6830916e66dc4a46a71368046b149b283de87769690e7aa4e0b9483530c
- Extracted: rgb/ (613 PNG, 640x480), groundtruth.txt, rgb.txt, accelerometer.txt (depth/ not extracted)
- Calibration: camera.json (Freiburg 1 RGB intrinsics/distortion from the official file_formats page)
- Citation: Sturm et al., "A Benchmark for the Evaluation of RGB-D SLAM Systems", IROS 2012.
- 2026-11-14: depth/ (595 PNG, 16-bit, 5000 = 1 m, pre-registered to rgb) extracted from the same archive with `tar -xzf rgbd_dataset_freiburg1_desk.tgz rgbd_dataset_freiburg1_desk/depth`; used only as independent ground truth for the metric-scale check (outputs/evidence/metric_scale/real_tum_fr1_desk.json), never for building the map.
