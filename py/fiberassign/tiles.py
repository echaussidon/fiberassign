# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-
"""
fiberassign.tiles
=====================

Functions for loading the tiles

"""
from __future__ import absolute_import, division, print_function

import re

import warnings

import numpy as np

from datetime import datetime

import desimodel
from desimodel.focalplane.fieldrot import field_rotation_angle

import astropy.time

from ._internal import Tiles


def load_tiles(tiles_file=None, select=None, obstime=None, obstheta=None):
    """Load tiles from a file.

    Load tile data either from the specified file or from the default provided
    by desimodel.  Optionally select a subset of tile IDs.

    Args:
        tiles_file (str):  Optional path to a FITS format footprint file.
        select (list):  List of tile IDs to keep when reading the file.
        obstime (str):  An ISO format string to override the observing time
            of all tiles.
        obstheta (float):  The angle in degrees to override the field rotation
            of all tiles.

    Returns:
        (Tiles):  A Tiles object.

    """
    # Read in the tile information
    if tiles_file is None:
        tiles_file = desimodel.io.findfile('footprint/desi-tiles.fits')
    tiles_data = desimodel.io.load_tiles(tilesfile=tiles_file, cache=False)

    keeprows = np.arange(len(tiles_data))
    if select is not None:
        keeprows = np.where(np.isin(tiles_data["TILEID"], select))[0]

    # astropy ERFA doesn't like the future
    warnings.filterwarnings(
        'ignore', message=
        r'ERFA function \"[a-z0-9_]+\" yielded [0-9]+ of \"dubious year')

    obsdate = None
    if obstime is not None:
        # We are overriding the observation date for all tiles.
        dtobs = None
        if re.match(".*:.*", obstime) is not None:
            dtobs = datetime.strptime(obstime, "%Y-%m-%dT%H:%M:%S")
        else:
            dtobs = datetime.strptime(obstime, "%Y-%m-%d")
        obsdate = [dtobs for x in range(len(keeprows))]
    elif "OBSDATE" in tiles_data.names:
        # We have the obsdate for every tile in the file.
        obsdate = [
            datetime.strptime(x, "%Y-%m-%dT%H:%M:%S")
            for x in tiles_data["OBSDATE"][keeprows]
        ]
    else:
        # We have no information.  Use the middle of the survey.
        obsdate = [
            datetime.strptime("2022-07-01", "%Y-%m-%d")
            for x in range(len(keeprows))
        ]

    obsdatestr = [x.isoformat() for x in obsdate]

    # Eventually, call a function from desimodel to query the field
    # rotation and hour angle for every tile time.

    if obstheta is None:
        theta_obs = list()
        for tra, tdec, ttime in zip(
                tiles_data["RA"][keeprows],
                tiles_data["DEC"][keeprows],
                obsdate):
            mjd = astropy.time.Time(ttime).mjd
            th = field_rotation_angle(tra, tdec, mjd)
            theta_obs.append(th)
        theta_obs = np.array(theta_obs)
    else:
        # support scalar or array obstheta inputs
        theta_obs = np.zeros(len(keeprows), dtype=np.float64)
        theta_obs[:] = obstheta

    # default to zero Hour Angle; may be refined later
    ha_obs = np.zeros(len(keeprows), dtype=np.float64)

    tls = Tiles(tiles_data["TILEID"][keeprows], tiles_data["RA"][keeprows],
                tiles_data["DEC"][keeprows],
                tiles_data["OBSCONDITIONS"][keeprows],
                obsdatestr, theta_obs, ha_obs)

    return tls
