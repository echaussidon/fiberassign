# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-
"""
fiberassign.targets
=====================

Functions for loading the target list

"""
from __future__ import absolute_import, division, print_function

import numpy as np

import fitsio

from desitarget.targetmask import desi_mask

from .utils import Logger, Timer

from ._internal import (TARGET_TYPE_SCIENCE, TARGET_TYPE_SKY,
                        TARGET_TYPE_STANDARD, TARGET_TYPE_SAFE,
                        Target, Targets, TargetTree, TargetsAvailable,
                        FibersAvailable)


def str_to_target_type(input):
    if input == "science":
        return TARGET_TYPE_SCIENCE
    elif input == "sky":
        return TARGET_TYPE_SKY
    elif input == "standard":
        return TARGET_TYPE_STANDARD
    elif input == "safe":
        return TARGET_TYPE_SAFE
    else:
        raise ValueError("unknown target type '{}'".format(input))
    return None

def get_sciencemask():
    '''Returns default mask for which DESI_TARGET bits are science targets'''
    sciencemask = 0
    sciencemask |= desi_mask["LRG"].mask
    sciencemask |= desi_mask["ELG"].mask
    sciencemask |= desi_mask["QSO"].mask
    # Note: BAD_SKY are treated as science targets with priority == 0
    sciencemask |= desi_mask["BAD_SKY"].mask
    sciencemask |= desi_mask["BGS_ANY"].mask
    sciencemask |= desi_mask["MWS_ANY"].mask
    sciencemask |= desi_mask["SECONDARY_ANY"].mask
    return sciencemask

def get_stdmask():
    '''Returns default mask for which DESI_TARGET bits are stdstar targets'''
    stdmask = 0
    stdmask |= desi_mask["STD_FAINT"].mask
    stdmask |= desi_mask["STD_WD"].mask
    stdmask |= desi_mask["STD_BRIGHT"].mask
    return stdmask

def get_skymask():
    '''Returns default mask for which DESI_TARGET bits are sky targets'''
    skymask = 0
    skymask |= desi_mask["SKY"].mask
    return skymask

def get_safemask():
    '''
    Returns default mask for which DESI_TARGET bits are backup/safe targets

    Note: these are targets of last resort; they are safe locations where
    we won't saturate the detector, but aren't good for anything else.
    '''
    safemask = 0
    safemask |= desi_mask.BAD_SKY
    return safemask

def get_excludemask():
    '''
    Returns default DESI_TARGET mask for bits that should not be observed
    '''
    excludemask = 0
    #- Exclude BRIGHT_OBJECT and IN_BRIGHT_OBJECT, but not NEAR_BRIGHT_OBJECT
    excludemask |= desi_mask.BRIGHT_OBJECT
    excludemask |= desi_mask.IN_BRIGHT_OBJECT
    return excludemask

def desi_target_type(desi_target, sciencemask=None, stdmask=None,
                     skymask=None, safemask=None, excludemask=None):
    """Determine fiber assign type from DESI_TARGET.
    """
    if sciencemask is None:
        sciencemask = get_sciencemask()

    if stdmask is None:
        stdmask = get_stdmask()

    if skymask is None:
        skymask = get_skymask()

    if safemask is None:
        safemask = get_safemask()

    if excludemask is None:
        excludemask = get_excludemask()

    if np.isscalar(desi_target):
        ttype = 0
        if desi_target & sciencemask != 0:
            ttype |= TARGET_TYPE_SCIENCE
        if desi_target & stdmask != 0:
            ttype |= TARGET_TYPE_STANDARD
        if desi_target & skymask != 0:
            ttype |= TARGET_TYPE_SKY
        if desi_target & safemask != 0:
            ttype |= TARGET_TYPE_SAFE
        if desi_target & excludemask != 0:
            ttype = 0
    else:
        desi_target = np.asarray(desi_target)
        ttype = np.zeros(len(desi_target), dtype=int)
        ttype[desi_target & sciencemask != 0] |= TARGET_TYPE_SCIENCE
        ttype[desi_target & stdmask != 0] |= TARGET_TYPE_STANDARD
        ttype[desi_target & skymask != 0] |= TARGET_TYPE_SKY
        ttype[desi_target & safemask != 0] |= TARGET_TYPE_SAFE
        ttype[desi_target & excludemask != 0] = 0

    return ttype


def append_target_table(tgs, tgdata, typeforce=None, typecol=None,
                        sciencemask=None, stdmask=None, skymask=None,
                        safemask=None, excludemask=None):
    validtypes = [
        TARGET_TYPE_SCIENCE,
        TARGET_TYPE_SKY,
        TARGET_TYPE_STANDARD,
        TARGET_TYPE_SAFE
    ]
    if typeforce is not None:
        if typeforce not in validtypes:
            raise RuntimeError("Cannot force objects to be an invalid type")
    # Create buffers for column data
    nrows = len(tgdata["TARGETID"])
    d_obscond = np.empty(nrows, dtype=np.int32)
    d_targetid = np.empty(nrows, dtype=np.int64)
    d_ra = np.empty(nrows, dtype=np.float64)
    d_dec = np.empty(nrows, dtype=np.float64)
    d_type = np.empty(nrows, dtype=np.uint8)
    d_nobs = np.empty(nrows, dtype=np.int32)
    d_prior = np.empty(nrows, dtype=np.int32)
    d_subprior = np.empty(nrows, dtype=np.float64)
    d_targetid[:] = tgdata["TARGETID"]
    if "TARGET_RA" in tgdata.dtype.names:
        d_ra[:] = tgdata["TARGET_RA"]
    else:
        d_ra[:] = tgdata["RA"]
    if "TARGET_DEC" in tgdata.dtype.names:
        d_dec[:] = tgdata["TARGET_DEC"]
    else:
        d_dec[:] = tgdata["DEC"]
    if typeforce is not None:
        d_type[:] = typeforce
    else:
        if typecol is None:
            # This table must already have FBATYPE
            if "FBATYPE" not in tgdata.dtype.names:
                raise RuntimeError("FBATYPE column not found- specify typecol")
            d_type[:] = tgdata["FBATYPE"]
        else:
            d_type[:] = desi_target_type(tgdata[typecol],
                            sciencemask=sciencemask, stdmask=stdmask,
                            skymask=skymask, safemask=safemask,
                            excludemask=excludemask)

    if "OBSCONDITIONS" in tgdata.dtype.fields:
        d_obscond[:] = tgdata["OBSCONDITIONS"]
    else:
        # Set obs conditions mask to be all bits
        d_obscond[:] = np.invert(np.zeros(nrows, dtype=np.int32))

    if "NUMOBS_MORE" in tgdata.dtype.fields:
        d_nobs[:] = tgdata["NUMOBS_MORE"]
    else:
        d_nobs[:] = np.zeros(nrows, dtype=np.int32)

    if "PRIORITY" in tgdata.dtype.fields:
        d_prior[:] = tgdata["PRIORITY"]
    else:
        d_prior[:] = np.zeros(nrows, dtype=np.int32)

    if "SUBPRIORITY" in tgdata.dtype.fields:
        d_subprior[:] = tgdata["SUBPRIORITY"]
    else:
        d_subprior[:] = np.zeros(nrows, dtype=np.float64)

    # Append the data to our targets list.  This will print a
    # warning if there are duplicate target IDs.
    tgs.append(d_targetid, d_ra, d_dec, d_nobs, d_prior, d_subprior,
               d_obscond, d_type)
    return


def load_target_file(tgs, tfile, typeforce=None, typecol="DESI_TARGET",
                     sciencemask=None, stdmask=None, skymask=None,
                     safemask=None, excludemask=None, rowbuffer=100000):
    tm = Timer()
    tm.start()

    log = Logger.get()

    # Open file
    fits = fitsio.FITS(tfile, mode="r")

    # Total number of rows
    nrows = fits[1].get_nrows()

    log.info("Target file {} has {} rows.  Reading in chunks of {}"
             .format(tfile, nrows, rowbuffer))

    offset = 0
    n = rowbuffer
    while offset < nrows:
        if offset + n > nrows:
            n = nrows - offset
        data = fits[1].read(rows=np.arange(offset, offset+n, dtype=np.int64))
        log.debug("Target file {} read rows {} - {}"
                  .format(tfile, offset, offset+n-1))
        append_target_table(tgs, data, typeforce=typeforce, typecol=typecol,
                            sciencemask=sciencemask, stdmask=stdmask,
                            skymask=skymask, safemask=safemask,
                            excludemask=excludemask)
        offset += n

    tm.stop()
    tm.report("Read target file {}".format(tfile))

    return
