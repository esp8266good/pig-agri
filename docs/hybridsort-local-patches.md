# HybridSORT 本地修改紀錄

`ref/HybridSORT/`（MOT 推論用的 vendored 專案）被 `.gitignore` 排除，不進本 repo 的版控。
這份文件記錄我們對它做過的所有本地修改，方便日後重裝 / 升級時重新套用。

所有修改都在單一檔案：
`ref/HybridSORT/trackers/hybrid_sort_tracker/hybrid_sort_reid.py`

對照的外部參數設定在本 repo 的 `inference/tracker_pool.py` 的 `_build_tracker_args()`
（`max_id_num`、`max_age`、`lost_track_buffer`、`lost_pool_max`、`reid_revive_thresh`）。

---

## 修改 1（2026-05-12）：「ID 上限」判斷改用活躍 tracker 數，而非累積計數

**問題**：原始上游程式在「是否能建立新 tracker」的判斷用 `KalmanBoxTracker.count`
（class 級計數器，**只增不減**，累計啟動以來建立過的所有 track）。豬被遮擋 → tracklet
在 `max_age` 幀後被刪 → 重新出現要建新 tracker → `count` 又 +1。一旦 `count` 累積到
`max_id_num`（`inference/tracker_pool.py` 設 40），就**永久禁止**建立新 tracker，掉追蹤的豬
再也救不回，只能重啟程式。

**修正**：`Hybrid_Sort_ReID.update()` 內兩處判斷，從 `KalmanBoxTracker.count >= self.max_id_num`
改為 `len(self.trackers) >= self.max_id_num`（用「目前活躍 tracker 數」，符合 `max_id_num`
的原意：場景最多同時 N 隻）。

- 位置 A：第二輪 IoU 重配對之後那段自訂「Extra Matching Rounds」的進入條件。
- 位置 B：`# create and initialise new trackers for unmatched detections` 迴圈內，
  建 `KalmanBoxTracker` 之前加 `if len(self.trackers) >= self.max_id_num: continue`。

```python
# create and initialise new trackers for unmatched detections
for i in unmatched_dets:
    if len(self.trackers) >= self.max_id_num:
        continue
    trk = KalmanBoxTracker(dets[i, :], id_feature_keep[i, :], delta_t=self.delta_t, args=self.args)
    self.trackers.append(trk)
```

---

## 修改 2（2026-05-13）：ReID 失蹤軌跡庫（解 ID 號碼暴增）

**問題**：修改 1 治好「掉追蹤」，但 `KalmanBoxTracker.count` 仍只增不減 —— 30 隻豬反覆遮擋
進出後 ID 號碼會飆到 600+。本 repo 的 `analysis/scheduler.py` 按 `object_id` 串接 bbox 中心點
位移算活動量，ID 一換、那隻豬的軌跡就被切成多段、每段樣本不足 `anomaly_min_samples` →
採血判斷失準。HybridSORT-ReID 本身沒有「失蹤軌跡庫」，track 超過 `max_age` 就在 `update()`
末尾直接 `self.trackers.pop(i)`，重新出現只能拿新 ID。

**修正 2a — `max_age` 接上並調大**：`Hybrid_Sort_ReID.__init__(self, args, det_thresh, max_age=30, ...)`
的 `max_age` 原本沒被 `tracker_pool.py` 傳值，一直是預設 30 幀（pipeline 10fps ≈ 3 秒）。
本 repo `inference/tracker_pool.py` 改為設 `args.max_age = 300` 並在建構時傳入 `max_age=`。
（vendored 檔案本身不需改；只是把既有參數用起來。`args.track_buffer` 仍是沒人讀的死參數。）

**修正 2b — 失蹤軌跡庫（改 vendored 檔案）**：

(1) `KalmanBoxTracker` 新增 `re_activate()` 方法（放在 `update()` 與 `predict()` 之間）：

```python
def re_activate(self, bbox, id_feature, min_hits=3):
    """Recover a tracklet that was removed after a long occlusion: keep the
    original id and the appearance memory (smooth_feat / features deque),
    but reset the motion + temporal state to the new detection so it behaves
    like a fresh, already-confirmed track. Used by the ReID lost-track pool."""
    self.kf.x[:5] = convert_bbox_to_z(bbox)
    self.kf.x[5:] = 0.
    self.kf.P *= 10.
    self.time_since_update = 0
    self.age = 0
    self.history = []
    self.history_observations = [bbox]
    self.observations = {0: bbox}
    self.last_observation = bbox
    self.last_observation_save = bbox
    self.velocity_lt = None
    self.velocity_rt = None
    self.velocity_lb = None
    self.velocity_rb = None
    self.hits += 1
    self.hit_streak = min_hits
    self.confidence_pre = None
    self.confidence = bbox[-1]
    self.update_features(id_feature)
```

(2) `Hybrid_Sort_ReID.__init__()` 末尾（`self.max_id_num = ...` 之後）新增：

```python
# ── ReID 失蹤軌跡庫 ──
self.lost_tracks = []
self.lost_track_buffer = getattr(self.args, 'lost_track_buffer', 1200)
self.lost_pool_max = getattr(self.args, 'lost_pool_max', 100)
self.reid_revive_thresh = getattr(self.args, 'reid_revive_thresh', 0.3)
```

(3) `Hybrid_Sort_ReID` 新增兩個 helper（放在 `camera_update()` 之後）：

```python
def _prune_lost_tracks(self):
    if not self.lost_tracks:
        return
    self.lost_tracks = [
        lt for lt in self.lost_tracks
        if self.frame_count - lt.lost_at <= self.lost_track_buffer
    ]
    if len(self.lost_tracks) > self.lost_pool_max:
        self.lost_tracks.sort(key=lambda lt: lt.lost_at)
        self.lost_tracks = self.lost_tracks[-self.lost_pool_max:]

def _associate_lost(self, dets, id_feature_keep, unmatched_dets):
    """Before minting brand-new ids, try to re-activate lost tracklets by
    appearance embedding so a reappearing object keeps its original id.
    Returns the (possibly reduced) unmatched_dets array."""
    if len(unmatched_dets) == 0 or not self.lost_tracks:
        return unmatched_dets
    left_feats = id_feature_keep[unmatched_dets]
    lost_feats = np.asarray([lt.smooth_feat for lt in self.lost_tracks], dtype=float)
    emb = embedding_distance(lost_feats, left_feats).T  # (n_dets, n_lost), cosine dist
    if emb.size == 0:
        return unmatched_dets
    matches = linear_assignment(emb)
    removed_dets = []
    removed_lost = set()
    for m in matches:
        di, li = int(m[0]), int(m[1])
        if li in removed_lost:
            continue
        if not np.isfinite(emb[di, li]) or emb[di, li] > self.reid_revive_thresh:
            continue
        real_det = int(unmatched_dets[di])
        revived = self.lost_tracks[li]
        revived.re_activate(dets[real_det, :], id_feature_keep[real_det, :], self.min_hits)
        self.trackers.append(revived)
        removed_dets.append(real_det)
        removed_lost.add(li)
    if removed_lost:
        self.lost_tracks = [lt for i, lt in enumerate(self.lost_tracks) if i not in removed_lost]
    if removed_dets:
        unmatched_dets = np.setdiff1d(unmatched_dets, np.array(removed_dets))
    return unmatched_dets
```

(4) `Hybrid_Sort_ReID.update()` 內三處接線：

- `self.frame_count += 1` 之後加 `self._prune_lost_tracks()`。
- `for m in unmatched_trks: self.trackers[m].update(None, None)` 之後、
  `# create and initialise new trackers` 之前加：
  `unmatched_dets = self._associate_lost(dets, id_feature_keep, unmatched_dets)`。
- 末尾刪 dead tracklet 那段，原本：

  ```python
  if(trk.time_since_update > self.max_age):
      self.trackers.pop(i)
  ```

  改為（先入庫再刪）：

  ```python
  if(trk.time_since_update > self.max_age):
      trk.lost_at = self.frame_count
      self.lost_tracks.append(trk)
      if len(self.lost_tracks) > self.lost_pool_max:
          self.lost_tracks.sort(key=lambda lt: lt.lost_at)
          self.lost_tracks = self.lost_tracks[-self.lost_pool_max:]
      self.trackers.pop(i)
  ```

**注意 / 調參**：
- 豬長得像時 ReID cosine 距離可能很近，`reid_revive_thresh` 太鬆會把兩隻豬合併成同一 id
  （比 ID churn 更糟，活動量會算到 frankenstein 軌跡）。寧可調更小（0.2 甚至更低）也別放鬆。
  若仍誤配，可在 `_associate_lost` 加「位置 gate」：lost track 最後 bbox 中心離 detection 太遠就跳過。
- 這些修改只在 `update()`（ReID 版）生效；`update_public()`（KITTI 版）沒動。
- `tests/test_tracker_pool.py` 仍 mock 掉 `Hybrid_Sort_ReID`，沒覆蓋上述邏輯；驗證靠手動 smoke
  腳本（直接建 `Hybrid_Sort_ReID`、模擬遮擋進出，確認重現的物體拿回原 id）。
