# SPDX-FileCopyrightText: 2026 LichtFeld Studio Authors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible colored initialization, bounded by per-view and global point budgets."""
from pathlib import Path
import math
import os
import uuid
import zipfile
import numpy as np
if __package__:
    from .capture_settings import validate_view_count
else:
    from capture_settings import validate_view_count


# Global initial point budget. Large captures (thousands of interior views) need far more than the
# historical 250 000 points: depth supervision requires 256 visible anchors in every single view.
MAX_SEED_POINTS = 10_000_000
# Own samples each view keeps in the merged cloud, so depth anchoring (256 required) cannot starve
# a view that sees only a small part of the scene (close walls, corridors, stairs).
ANCHORS_PER_VIEW = 320


def view_budget(limit, views):
    validate_view_count(views)
    if type(limit) is not int or not 4 <= limit <= MAX_SEED_POINTS:
        raise ValueError("Invalid initialization budget or capture view count")
    return max(256, min(4096, math.ceil(4 * limit / views)))


def anchor_budget(limit, views):
    """Own samples reserved per view inside the global budget (never more than an even share)."""
    validate_view_count(views)
    return min(ANCHORS_PER_VIEW, limit // views)


def minimum_depth_budget(views):
    """Smallest global budget that lets every view keep enough depth anchors."""
    validate_view_count(views)
    return ANCHORS_PER_VIEW * views


def instance_sample_plan(limit, instances):
    """Deterministically distribute a strict global surface-sample budget."""
    if type(limit) is not int or limit < 1 or type(instances) is not int or instances < 1:
        raise ValueError('Surface sampling needs a positive budget and instance count')
    selected = min(limit, instances)
    if instances <= limit:
        base, remainder = divmod(limit, instances)
        for index in range(instances):
            yield index, base + (index < remainder)
    else:
        # Evenly cover the evaluated dependency-graph sequence without retaining
        # an index list proportional to a potentially enormous instance count.
        for rank in range(limit):
            yield (rank * instances) // limit, 1


def valid_pixels(position, depth, rgba):
    height, width = depth.shape
    if position.shape != (height, width, 3) or rgba.shape != (height, width, 4):
        raise ValueError("Initialization passes have inconsistent dimensions")
    valid = (np.isfinite(depth) & (depth > 0) & (depth < 1e9) &
             np.isfinite(position).all(axis=2) & (rgba[:, :, 3] >= 250))
    # RGB is antialiased while numerical passes can describe a single surface.
    # Avoid foreground/background mixtures and depth discontinuities at outlines.
    base = valid.copy()
    for a, b in ((np.s_[1:, :], np.s_[:-1, :]), (np.s_[:-1, :], np.s_[1:, :]),
                 (np.s_[:, 1:], np.s_[:, :-1]), (np.s_[:, :-1], np.s_[:, 1:])):
        with np.errstate(invalid='ignore'):
            valid[a] &= base[b] & (np.abs(depth[a] - depth[b]) <= np.maximum(.05 * depth[a], 1e-5))
    return valid


def pixel_indices(valid, limit, index=0):
    """Select valid-pixel ranks without an int64 index for every image pixel."""
    counts = np.count_nonzero(valid, axis=1)
    cumulative = np.cumsum(counts)
    total = int(cumulative[-1])
    if total > limit:
        rng = np.random.default_rng(142857 + index)
        ranks = ((np.arange(limit) + rng.random(limit)) * total / limit).astype(np.int64)
    else:
        ranks = np.arange(total, dtype=np.int64)
    rows = np.searchsorted(cumulative, ranks, side='right')
    selected = np.empty(len(ranks), np.int64)
    unique, starts, lengths = np.unique(rows, return_index=True, return_counts=True)
    for row, start, length in zip(unique, starts, lengths):
        columns = np.flatnonzero(valid[row])
        prior = cumulative[row-1] if row else 0
        selected[start:start+length] = row * valid.shape[1] + columns[ranks[start:start+length]-prior]
    return selected


def samples(position, depth, rgba, matrix, frame, limit, index=0, valid=None):
    """Positions and encoded PNG colors refer to the same rendered pixel centers."""
    height, width = depth.shape
    if valid is None:
        valid = valid_pixels(position, depth, rgba)
    indices = pixel_indices(valid, limit, index)
    points = position.reshape(-1, 3)[indices]
    pose = np.asarray(matrix, dtype=np.float64)
    local = (points - pose[:3, 3]) @ pose[:3, :3]
    z = -local[:, 2]
    rows, cols = np.divmod(indices, width)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = local[:, 0] / z * frame['fl_x'] + frame['cx']
        v = -local[:, 1] / z * frame['fl_y'] + frame['cy']
    consistent = ((z > 0) & np.isfinite(u) & np.isfinite(v) &
                  (np.abs(u - cols - .5) <= 1.25) &
                  (np.abs(v - (height - rows - .5)) <= 1.25))
    return points[consistent].astype(np.float32), rgba.reshape(-1, 4)[indices[consistent], :3].copy()


def save_view(output, index, points, colors):
    directory = Path(output) / 'capture_points'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f'view_{index:06d}.npz'
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            np.savez(stream, points=points, colors=colors)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_view(output, index, limit):
    path = Path(output) / 'capture_points' / f'view_{index:06d}.npz'
    if path.is_symlink() or not path.resolve().is_relative_to(Path(output).resolve()):
        raise ValueError("Initialization samples cannot be a symbolic link")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if ({entry.filename for entry in entries} != {'points.npy', 'colors.npy'} or len(entries) != 2 or
                    sum(entry.file_size for entry in entries) > limit * 15 + 2048):
                raise ValueError("Invalid initialization sample archive")
            arrays = []
            for name, expected in (('points.npy', np.dtype(np.float32)), ('colors.npy', np.dtype(np.uint8))):
                with archive.open(name) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version not in {(1, 0), (2, 0)}:
                        raise ValueError('Unsupported initialization sample format')
                    header = (np.lib.format.read_array_header_1_0 if version == (1, 0) else
                              np.lib.format.read_array_header_2_0)
                    shape, fortran, dtype = header(stream, max_header_size=1024)
                    if (len(shape) != 2 or shape[1] != 3 or not 0 <= shape[0] <= limit or
                            dtype != expected or fortran):
                        raise ValueError('Invalid initialization sample dimensions or type')
                    length = shape[0] * 3 * dtype.itemsize
                    data = stream.read(length + 1)
                    if len(data) != length:
                        raise ValueError('Invalid initialization sample payload')
                    arrays.append(np.frombuffer(data, dtype=dtype).reshape(shape))
            points, colors = arrays
        if (points.ndim != 2 or points.shape[1:] != (3,) or len(points) > limit or
                colors.shape != points.shape or points.dtype != np.float32 or
                colors.dtype != np.uint8 or not np.isfinite(points).all()):
            raise ValueError("Invalid initialization samples")
        return points, colors
    except (FileNotFoundError, zipfile.BadZipFile) as error:
        raise ValueError(f"Visible-point samples for view {index + 1} are missing or incomplete. Existing rendered images are preserved.") from error


def merge(views, limit, reserve=0):
    """Deduplicate all view samples, then keep at most `limit` points. With `reserve`, each view first keeps
    up to `reserve` of its own samples (evenly spread over its image); spatial stratification fills the rest."""
    sizes = [len(value[0]) for value in views]
    points = np.concatenate([value[0] for value in views])
    colors = np.concatenate([value[1] for value in views])
    if len(points) < 4:
        raise ValueError("Too few visible surface points. Check camera coverage and transparent surfaces.")
    low, high = points.min(axis=0), points.max(axis=0)
    tolerance = max(float((high - low).max()) * 1e-6, 1e-7)
    keys = np.rint((points.astype(np.float64) - low) / tolerance).astype(np.int64)
    _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    # Average repeated surface/color observations before choosing spatial coverage.
    points = np.column_stack([np.bincount(inverse, weights=points[:, i]) / counts for i in range(3)]).astype(np.float32)
    colors = np.rint(np.column_stack([np.bincount(inverse, weights=colors[:, i]) / counts for i in range(3)])).astype(np.uint8)
    if len(points) < 4:
        raise ValueError("Fewer than four distinct visible points. Check capture coverage.")
    if len(points) > limit:
        from lichtfeld_sessions.capture import spatial_indices
        reserved = reserved_indices(sizes, inverse, reserve, limit)
        if len(reserved):
            rest = np.setdiff1d(np.arange(len(points)), reserved, assume_unique=True)
            fill = rest[spatial_indices(points[rest], limit - len(reserved))] if limit > len(reserved) else rest[:0]
            chosen = np.sort(np.concatenate((reserved, fill)))
        else:
            chosen = spatial_indices(points, limit)
        points, colors = points[chosen], colors[chosen]
    return points, colors


def reserved_indices(sizes, inverse, reserve, limit):
    """Merged-point indices of the samples each view keeps for itself (deterministic, bounded by `limit`)."""
    if reserve < 1:
        return np.zeros(0, np.int64)
    picks, offset = [], 0
    for size in sizes:
        if size:
            take = np.linspace(0, size - 1, min(size, reserve), dtype=np.int64)
            picks.append(inverse[offset + take])
        offset += size
    if not picks:
        return np.zeros(0, np.int64)
    reserved = np.unique(np.concatenate(picks)).astype(np.int64)
    if len(reserved) > limit:
        reserved = reserved[np.linspace(0, len(reserved) - 1, limit, dtype=np.int64)]
    return reserved


def write_ply(path, points, colors):
    records = np.empty(len(points), dtype=[('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))])
    records['xyz'], records['rgb'] = points, colors
    path = Path(path)
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write((f'ply\nformat binary_little_endian 1.0\nelement vertex {len(points)}\n'
                          'property float x\nproperty float y\nproperty float z\n'
                          'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n').encode('ascii'))
            stream.write(records.tobytes())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finalize(output, limit, count, return_cloud=False):
    budget = view_budget(limit, count)
    reserve = anchor_budget(limit, count)
    points, colors = merge([load_view(output, index, budget) for index in range(count)], limit, reserve)
    write_ply(Path(output) / 'pointcloud.ply', points, colors)
    info = {'method': 'visible_rendered', 'points': len(points), 'budget': limit,
            'color_space': 'encoded dataset RGB', 'samples_per_view': budget, 'reserved_per_view': reserve}
    return (info, points, colors) if return_cloud else info
