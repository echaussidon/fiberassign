'''
This is a little demo script for the Assignment.check_avail_collisions() function.
'''

import numpy as np

from astropy.table import Table

from fiberassign.hardware import load_hardware
from fiberassign.tiles import load_tiles
from fiberassign.targets import Targets, TargetsAvailable, LocationsAvailable, create_tagalong, load_target_file, targets_in_tiles
from fiberassign.assign import Assignment

from fiberassign.utils import Logger

import fitsio

def main():

    # from LSS.mkCat_singletile.fa4lsscat import getfatiles
    # getfatiles()
    # return
    log = Logger.get()

    margins = dict(pos=0.05,
                   petal=0.4,
                   gfa=0.4)

    hw = load_hardware(rundate=None, add_margins=margins)

    t = Table.read('/global/cfs/cdirs/desi/survey/catalogs/Y1/LSS/tiles-DARK.fits')
    print('tiles:', t)

    tile = 1230
    ts = '%06i' % tile

    fbah = fitsio.read_header('/global/cfs/cdirs/desi/target/fiberassign/tiles/trunk/'+ts[:3]+'/fiberassign-'+ts+'.fits.gz')

    pr = 'DARK'
    t['OBSCONDITIONS'] = 516
    t['IN_DESI'] = 1
    t['MTLTIME'] = fbah['MTLTIME']
    t['FA_RUN'] = fbah['FA_RUN']
    t['PROGRAM'] = pr

    t.write('tiles.fits', overwrite=True)

    tiles = load_tiles(
        tiles_file='tiles.fits',
        select=[tile])

    tids = tiles.id
    print('Tile ids:', tids)
    I = np.flatnonzero(np.array(tids) == tile)
    assert(len(I) == 1)
    i = I[0]
    tile_ra  = tiles.ra[i]
    tile_dec = tiles.dec[i]

    # Create empty target list
    tgs = Targets()
    # Create structure for carrying along auxiliary target data not needed by C++.
    plate_radec=True
    tagalong = create_tagalong(plate_radec=plate_radec)

    # Load target files...
    load_target_file(tgs, tagalong, '/global/cfs/cdirs/desi/survey/catalogs/main/LSS/random0/tilenofa-%i.fits' % tile)

    # Find targets within tiles, and project their RA,Dec positions
    # into focal-plane coordinates.
    tile_targetids, tile_x, tile_y, tile_xy_cs5 = targets_in_tiles(hw, tgs, tiles, tagalong)
    # Compute the targets available to each fiber for each tile.
    tgsavail = TargetsAvailable(hw, tiles, tile_targetids, tile_x, tile_y)
    # Compute the fibers on all tiles available for each target and sky
    favail = LocationsAvailable(tgsavail)

    # FAKE stucksky
    stucksky = {}

    # Create assignment object
    asgn = Assignment(tgs, tgsavail, favail, stucksky)

    coll = asgn.check_avail_collisions(tile)

    #print('collisions:', coll)
    print('N collisions:', len(coll))
    # coll: dict (loc, targetid) -> bitmask

    # For plotting: collect bitmasks for all collisions (even non-collisions)
    coll = asgn.check_avail_collisions(tile, True)
    print('Incl non-collisions:', len(coll))

    # All positioner locations -> x,y
    loc_to_cs5 = hw.loc_pos_cs5_mm;
    # target -> x,y
    target_to_xy = dict()
    for t,x,y in zip(tile_targetids[tile], tile_x[tile], tile_y[tile]):
        target_to_xy[t] = (x,y)

    goodx = []
    goody = []
    badx = []
    bady = []
    badval = []

    for k,v in coll.items():
        loc,targetid = k
        bitmask = v

        lxy = loc_to_cs5[loc]
        txy = target_to_xy[targetid]

        if bitmask == 0:
            goodx.append([lxy[0], txy[0]])
            goody.append([lxy[1], txy[1]])
        else:
            badx.append([lxy[0], txy[0]])
            bady.append([lxy[1], txy[1]])
            badval.append(bitmask)

    import pylab as plt

    plt.clf()
    plt.plot(np.array(goodx).T, np.array(goody).T, '-', color='k', alpha=0.1)

    badval = np.array(badval)
    badx = np.array(badx)
    bady = np.array(bady)
    for val,cc in zip([1,2,4], ['r','m','k']):
        I = np.flatnonzero(val & badval)
        plt.plot(badx[I,:].T, bady[I,:].T, '-', color=cc)
    plt.savefig('collide.png')

    plt.axis([0, 200, -400, -200])
    plt.savefig('collide2.png')

if __name__ == '__main__':
    main()
