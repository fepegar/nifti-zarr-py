import argparse
import io
import sys
from argparse import ArgumentDefaultsHelpFormatter
from os import PathLike
from typing import Any, Literal, Optional, Union

import dask.array
import numpy as np
import zarr.storage
from nibabel import (save, load)
from nibabel.nifti1 import Nifti1Image, Nifti1Header
from nibabel.nifti2 import Nifti2Image, Nifti2Header

from ._compat import _open_zarr
from ._header import bin2nii, get_nibabel_klass
from ._units import convert_unit, ome_valid_units


def _ome2affine(ome, level=0):
    names = [axis["name"] for axis in ome[0]["axes"]]
    units = [axis.get("unit", None) for axis in ome[0]["axes"]]
    scales, offsets = None, None
    for trf in ome[0]["datasets"][level]["coordinateTransformations"]:
        if trf["type"] == "scale":
            scales = trf["scale"]
            if offsets:
                # not valid OME but let's be robust
                offsets = [t * s for t, s in zip(offsets, scales)]
        elif trf["type"] == "translation":
            offsets = trf["translation"]
    scales = scales or [1.0] * len(names)
    offsets = offsets or [0.0] * len(names)
    scales = [
        convert_unit(x, unit, "mm")
        if unit in ome_valid_units["space"] else
        convert_unit(x, unit, "s")
        if unit in ome_valid_units["time"] else
        x for x, unit in zip(scales, units)
    ]
    offsets = [
        convert_unit(x, unit, "mm")
        if unit in ome_valid_units["space"] else
        convert_unit(x, unit, "s")
        if unit in ome_valid_units["time"] else
        x for x, unit in zip(offsets, units)
    ]
    scales = {name: scales[i] for i, name in enumerate(names)}
    offsets = {name: offsets[i] for i, name in enumerate(names)}

    # make affine
    affine = np.eye(4)
    affine[range(3), range(3)] = [
        scales.get(name, 1.0) for name in "xyz"
    ]
    affine[:3, -1] = [
        offsets.get(name, 0.0) for name in "xyz"
    ]

    return affine


def default_nifti_header(inp0: zarr.Array, ome: dict):
    """
    Generate a default nifti header.

    Parameters
    ----------
    inp0: zarr.Array
        Input array.
    ome: dict
        ome-zarr metadata.
    """
    # not a nifti-zarr -> create nifti header on the fly
    if any(x > 2 ** 15 for x in inp0.shape):
        NiftiHeader = Nifti2Header
    else:
        NiftiHeader = Nifti1Header
    niiheader = NiftiHeader()
    if ome:
        affine = _ome2affine(ome)

        # make shape
        names = [axis["name"] for axis in ome[0]["axes"]]
        shape = {name: inp0.shape[i] for i, name in enumerate(names)}
        shape = [shape.get(name, 1) for name in "xyztc"]
        if "c" not in names:
            shape = shape[:4]
            if "t" not in names:
                shape = shape[:3]
                if "z" not in names:
                    shape = shape[:2]
                    if "y" not in names:
                        shape = shape[:1]
                        if "x" not in names:
                            shape = shape[:0]

    else:
        # not an OME zarr -- assume order [t, c, z, y, x] nonetheless
        affine = np.eye(4)
        shape = list(inp0.shape)[::-1]
        shape_dict = {k: v for k, v in zip("xyzct", shape)}
        shape = list(shape_dict.values()) + shape[5:]
        if len(shape) > 4:
            # permute c <-> t
            shape[3], shape[4] = shape[4], shape[3]
    # set nifti fields
    niiheader.set_data_shape(shape)
    niiheader.set_data_dtype(inp0.dtype)
    niiheader.set_qform(affine)
    niiheader.set_sform(affine)
    niiheader.set_xyzt_units("mm", "sec")
    return niiheader


def zarr2nii(
        inp: Union[str, PathLike, Any],
        out: Optional[Union[str, PathLike]] = None,
        level: Union[int, str] = 0,
        mode: Literal["r", "w", "a"] = "r",
        **store_opt
) -> Union[Nifti1Image, Nifti2Image]:
    """
    Convert a nifti-zarr to nifti

    Parameters
    ----------
    inp : zarr.Store | zarr.Group | zarr.Array | path
        Output zarr object
    out : path or file_like, optional
        Path to output file. If not provided, do not write a file.
    level : int
        Pyramid level to extract
    mode : {"r", "w", "a"}
        Opening mode.

    Returns
    -------
    out : nib.Nifti1Image
        Mapped output file _or_ Nifti object whose dataobj is a dask array
    """

    inp = _open_zarr(inp, mode=mode, store_opt=store_opt)

    # ----------------
    # prepare metadata
    # ----------------

    # Compute number of levels
    if isinstance(inp, zarr.Group):
        is_group = True
        inp0 = inp["0"]
        nb_levels = 0
        while str(nb_levels) in inp.keys():
            nb_levels += 1
        if nb_levels == 0:
            raise ValueError("This is a Zarr group but not an OME-Zarr.")
        if level < 0:
            level = nb_levels + level
        if level >= nb_levels:
            raise IndexError(
                "Pyramid level does not exist. Number of levels:",
                nb_levels
            )
    else:
        is_group = False
        inp0 = inp
        if level not in (0, -1):
            raise IndexError("Pyramid level does not exist -- not an OME zarr")

    # get OME metadata (if exists)
    ome = inp.attrs.get("ome", inp.attrs).get("multiscales", None)

    # --------------------------
    # read or build nifti header
    # --------------------------

    if not is_group or 'nifti' not in inp:
        niiheader = default_nifti_header(inp0, ome)
        if isinstance(niiheader, Nifti2Header):
            NiftiImage = Nifti2Image
        elif isinstance(niiheader, Nifti1Header):
            NiftiImage = Nifti1Image
        else:
            raise ValueError("Unrecognized nifti header.")
    else:
        header = bin2nii(np.asarray(inp['nifti']).tobytes())
        NiftiHeader, NiftiImage = get_nibabel_klass(header)

        niiheader = NiftiHeader.from_fileobj(
            io.BytesIO(np.asarray(inp['nifti']).tobytes()),
            check=False)

    # -----------------------------------
    # create affine at current resolution
    # -----------------------------------

    if level != 0:

        qform, qcode = niiheader.get_qform(coded=True)
        sform, scode = niiheader.get_sform(coded=True)
        datasets = ome[0]['datasets']

        xfrm0 = datasets[0]['coordinateTransformations']
        scales, offsets = [], []
        for xfrm_ in xfrm0:
            if xfrm_["type"] == "scale":
                scales = xfrm_["scale"]
                if offsets:
                    # not valid OME but let's be robust
                    offsets = [t * s for t, s in zip(offsets, scales)]
            elif xfrm_["type"] == "translation":
                offsets = xfrm_["translation"]

        phys0 = np.eye(4)
        phys0[[0, 1, 2], [0, 1, 2]] = list(reversed(scales[-3:]))
        if offsets:
            phys0[:3, -1] = list(reversed(offsets[-3:]))

        xfrm1 = datasets[level]['coordinateTransformations']
        scales, offsets = [], []
        for xfrm_ in xfrm1:
            if xfrm_["type"] == "scale":
                scales = xfrm_["scale"]
                if offsets:
                    # not valid OME but let's be robust
                    offsets = [t * s for t, s in zip(offsets, scales)]
            elif xfrm_["type"] == "translation":
                offsets = xfrm_["translation"]

        phys1 = np.eye(4)
        phys1[[0, 1, 2], [0, 1, 2]] = list(reversed(scales[-3:]))
        if offsets:
            phys1[:3, -1] = list(reversed(offsets[-3:]))

        if qform is not None:
            qform = qform @ (np.linalg.inv(phys0) @ phys1)
            niiheader.set_qform(qform, qcode)
        if sform is not None:
            sform = sform @ (np.linalg.inv(phys0) @ phys1)
            niiheader.set_sform(sform, scode)

    # load/map array with dask
    if is_group:
        array = dask.array.from_zarr(inp[f'{level}'])
    else:
        array = dask.array.from_zarr(inp)

    # -------------------------------
    # reorder/reshape array as needed
    # -------------------------------

    # get zarr axes
    if ome:
        actual_axis_order = tuple(axis['name'] for axis in ome[0]['axes'])
    else:
        actual_axis_order = ('x', 'y', 'z', 'c', 't')[:len(inp0.shape)][::-1]

    # add axes if needed
    nifti_ndim = len(niiheader.get_data_shape())
    slicer = (Ellipsis,) + (None,) * max(0, 5 - array.ndim)
    array = array[slicer]

    # permute axes to nifti order (x, y, z, t, c)
    perm, i = [], len(actual_axis_order)
    for name in 'xyztc':
        if name in actual_axis_order:
            perm += [actual_axis_order.index(name)]
        else:
            perm += [i]
            i += 1

    array = array.transpose(perm)

    # drop axes
    slicer = (slice(None),) * nifti_ndim + (0,) * (array.ndim - nifti_ndim)
    array = array[slicer]

    # create nibabel image
    img = NiftiImage(array, niiheader.get_best_affine(), niiheader)

    if out is not None:
        if hasattr(out, 'read') and hasattr(img, "to_stream"):
            img.to_stream(out)
        else:
            save(img, out)
            img = load(out)

    return img


def cli(args=None):
    """Command-line entrypoint"""
    parser = argparse.ArgumentParser(
        'zarr2nii', description='Convert nifti to nifti-zarr.',
        formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        'input', help='Input zarr directory.')
    parser.add_argument(
        'output', default=None, nargs="?",
        help='Output nifti file. '
             'When not provided, write to the same directory as input.')
    parser.add_argument(
        '--level', type=int, default=0,
        help='Pyramid level to extract (default: 0 = finest).')

    args = args or sys.argv[1:]
    args = parser.parse_args(args)
    if args.output is None:
        if args.input.endswith('/'):
            args.input = args.input[:-1]
        if args.input.endswith('.nii.zarr'):
            args.output = args.input[:-9] + '.nii.gz'
        elif args.input.endswith('.ome.zarr'):
            args.output = args.input[:-8] + '.nii.gz'
        elif args.input.endswith('.zarr'):
            args.output = args.input[:-5] + '.nii.gz'
        else:
            args.output = args.input + '.nii.gz'
    zarr2nii(args.input, args.output, args.level)
