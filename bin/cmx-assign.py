#!/usr/bin/env python

'''
need to run before
source /global/cfs/cdirs/desi/software/desi_environment.sh master
'''

import os
import sys
import numpy as np
from glob                          import glob
from astropy.io                    import fits
from astropy.table                 import Table
import fitsio
from desitarget.io                 import read_targets_in_tiles,write_targets,write_mtl
from desitarget.cmx.cmx_targetmask import cmx_mask
from desitarget.targetmask         import obsconditions
from desitarget.targets            import set_obsconditions
from desimodel.footprint           import is_point_in_desi
import desimodel.io as dmio
from fiberassign.scripts.assign    import parse_assign,run_assign_bytile,run_assign_full
from fiberassign.scripts.merge     import parse_merge,run_merge
from fiberassign.utils             import Logger
from time                          import time
from datetime                      import datetime
import matplotlib.pyplot as plt
from matplotlib                    import gridspec
import matplotlib
from astropy                       import units
from astropy.coordinates           import SkyCoord
from argparse                      import ArgumentParser

'''
flavor : see DJS email Oct,15 2020:
	1) "we're lost" dither sequences
	2) dither sequences of bright stars (including w/ no offsets)
	3) standard star + sky tiles (these stars are fainter than the dither stars)
	4) few tiles with a mix of all science targets (for pipeline re-commissioning)
	5) focus dither sequences -- M. Lampton, E. Schlafly figuring this out
'''

'''
TBD : 
	- validate the code for tiles outside desi footprint, for dithering
	- add epoch as input (see Eddie email to desi-survey from Oct., 30, 2020)
	- "starfaint" flavor: pick other targets than STD_FAINT,SV0_WD, which are not enough
	- "science" flavor, dark: verify the elg-selection
'''

# AR to speed up development/debugging
dotile= True
dosky = True
dostd = True
dogfa = True
dotarg= True
dofa  = True
doplot= True


# AR reading arguments
parser = ArgumentParser()
parser.add_argument('--outdir',  help='output directory',type=str,default=None,required=True,metavar='OUTDIR')
parser.add_argument('--tileid',  help='output tileid (e.g., 63142); if flavor=dithprec,dithlost, will also write outputs tileid for the 12,5 next tileids',type=int,default=None,required=True,metavar='TILEID')
parser.add_argument('--intileid',help='input tileid from $DESIMODEL/data/footprint/desi-tiles.fits (e.g., 7160)',type=int,default=None,required=False,metavar='INTILEID')
parser.add_argument('--tilera',  help='tile centre ra  (required if intileid not provided)',type=float,default=None,required=False,metavar='TILERA')
parser.add_argument('--tiledec', help='tile centre dec (required if intileid not provided)',type=float,default=None,required=False,metavar='TILEDEC')
parser.add_argument('--flavor',  help='dithprec,dithlost,starfaint,science,focus',type=str,default=None,required=True,metavar='FLAVOR')
parser.add_argument('--rundate', help='rundate for focalplane (default=2020-03-06T00:00:00)',type=str,default='2020-03-06T00:00:00',required=False,metavar='RUNDATE')
parser.add_argument('--obscon',  help='dark or bright or "dark,bright"',type=str,default=None,required=True,metavar='OBSCON')
parser.add_argument('--dr',      help='legacypipe dr (default=dr8)',type=str,default='dr8',required=False,metavar='DR')
parser.add_argument('--dtver',   help='desitarget catalogue version',type=str,default=None,required=True,metavar='DTVER')
parser.add_argument('--seed',    help='numpy random seed for dithering (default=1234)',type=int,default=1234,required=False,metavar='SEED')
parser.add_argument('--doclean', help='delete tileid-{tiles,sky,std,gfa,targ}.fits files (y/n)',type=str,default='n',required=False,metavar='DOCLEAN')
#
args     = parser.parse_args()
log      = Logger.get()


start    = time()
log.info('{:.1f}s\tstart'.format(time()-start))

# AR safe: outdir
if (args.outdir[-1]!='/'):
	args.outdir += '/'
if (os.path.isdir(args.outdir)==False):
	os.mkdir(args.outdir)
# AR safe: tilera, tiledec
if ((args.tilera is None) | (args.tiledec is None)):
	if (args.intileid is None):
		log.error('{:.1f}s\teither (args.tilera,args.tiledec) or args.intileid should be provided; exiting'.format(time()-start))
		sys.exit()
	else:
		fn   = os.getenv('DESIMODEL')+'/data/footprint/desi-tiles.fits'
		d    = fits.open(fn)[1].data
		keep = (d['TILEID']==args.intileid)
		if (keep.sum()>0):
			args.tilera = d['RA'][keep][0]
			log.info('{:.1f}s\t{:.0f} in {} -> setting args.tilera ={}'.format(time()-start,args.intileid,fn,d['RA'][keep][0]))
			args.tiledec = d['DEC'][keep][0]
			log.info('{:.1f}s\t{:.0f} in {} -> setting args.tiledec={}'.format(time()-start,args.intileid,fn,d['DEC'][keep][0]))
		else:
			log.error('{:.1f}s\targs.intileid not in {}; exiting'.format(time()-start,fn))
			sys.exit()
# AR safe: flavor
if (args.flavor not in ['dithprec','dithlost','starfaint','science','focus']):
	log.error('args.flavor not in dithprec,dithlost,starfaint,science,focus; exiting')
	sys.exit()
if (args.obscon not in ['dark','bright','dark,bright']):
	log.error('args.obscon not in dark or bright or "dark,bright"; exiting')
	sys.exit()

root    = '{}{:06d}'.format(args.outdir,args.tileid)


# AR dictionary with settings proper to each flavor
fdict = {}
# AR minimum number of sky fibres per petal
fdict['nskypet'] = '40' # AR default
# AR (no) dithering initialisation
fdict['seed']    = -1	# AR random seed
fdict['ndither'] = 0	# AR number of dithered tiles
fdict['gfrac']   = 0.0	# AR fraction of assigned stars that are dithered with a Gaussian
fdict['gwidth']  = 0.0	# AR offset [arcsec] per coordinate normal distribution
fdict['bfrac']   = 0.0	# AR fraction of assigned stars  that are dithered within a box for the dithlost-in-space case
fdict['bwidth']  = 0.0	# AR box width [arcsec] of the dithering for the dithlost-in-space case



# AR is tile in the desi footprint?
# AR -> if not, special msk and targdir for dithering
tile_in_desi = is_point_in_desi(dmio.load_tiles(),args.tilera,args.tiledec).astype(int)
	


# AR flavor settings
if (args.flavor=='dithprec'):
	if (tile_in_desi==0):
		fdict['msks']= 'STD_DITHER_GAIA'
	else:
		fdict['msks']= 'STD_DITHER'
	fdict['seed']    = args.seed
	fdict['ndither'] = 12
	fdict['gfrac']   = 1.0
	fdict['gwidth']  = 0.7
elif (args.flavor=='dithlost'):
	if (tile_in_desi==0):
		fdict['msks']= 'STD_DITHER_GAIA'
	else:
		fdict['msks']= 'STD_DITHER'
	fdict['seed']    = args.seed
	fdict['ndither'] = 5
	fdict['gfrac']   = 0.5
	fdict['gwidth']  = 2.0
	fdict['bfrac']   = 0.5
	fdict['bwidth']  = 10.
elif (args.flavor=='starfaint'):
	fdict['msks']   = 'STD_FAINT,SV0_WD'
	fdict['nskypet']= '100'
elif (args.flavor=='science'):
# 	if (args.obscon=='dark') : fdict['msks'] = 'SV0_WD,MINI_SV_LRG,MINI_SV_ELG,MINI_SV_QSO'
# 	else:                      fdict['msks'] = 'SV0_WD,MINI_SV_BGS_BRIGHT,SV0_MWS_FAINT'
	if (args.obscon=='dark') : fdict['msks'] = 'SV0_WD,SV0_LRG,SV0_ELG,SV0_QSO'
	else:                      fdict['msks'] = 'SV0_WD,SV0_BGS,SV0_MWS_FAINT'
elif (args.flavor=='focus'):
	sys.exit('not implemented yet; exiting')
if (fdict['seed']!=-1):
	np.random.seed(fdict['seed'])



# AR directories
hostname = os.getenv('HOSTNAME')
if 'desi' in hostname:
        path_to_targets = '/data/target/catalogs/'
if 'cori' in hostname:
        path_to_targets = '/global/cfs/projectdirs/desi/target/catalogs'

mydirs = {}
if ((args.flavor in ['dithprec','dithlost']) & (tile_in_desi==0)):
	mydirs['targ']= os.path.join(path_to_targets, 'gaiadr2', args.dtver, 'targets/cmx/resolve/supp')
else:
	mydirs['targ']= os.path.join(path_to_targets, args.dr,args.dtver,'targets/cmx/resolve/no-obscon')
mydirs['sky']     = os.path.join(path_to_targets, args.dr, args.dtver, 'skies')
mydirs['skysupp'] = os.path.join(path_to_targets, 'gaiadr2', args.dtver, 'skies-supp')
mydirs['gfa']     = os.path.join(path_to_targets, args.dr, args.dtver, 'gfas')
for key in mydirs.keys():
	log.info('{:.1f}s\tdirectory for {}: {}'.format(time()-start,key,mydirs[key]))



# AR extra tileid if ndither>0; switching to np.array() in all cases
tileids = np.array([args.tileid+i for i in range(1+fdict['ndither'])])
log.info('{:.1f}s\twill process {} tiles with tileid={}'.format(time()-start,1+fdict['ndither'],','.join([str(tileid) for tileid in tileids])))
# AR safe tileids
prev_fns   = [fn.split('/')[-1] for fn in 
					glob(os.getenv('DESI_TARGET')+'/fiberassign/tiles/trunk/???/fiberassign-??????.fits')]
new_fns    = ['fiberassign-{:06d}.fits'.format(tid) for tid in tileids]
if (np.in1d(new_fns,prev_fns).sum()>0):
    log.error('{:.1f}s\tsome of {} files already exist; exiting'.format(time()-start,','.join(new_fns)))
    sys.exit()



# AR printing settings
tmpstr  = ' , '.join([kwargs[0]+'='+str(kwargs[1]) for kwargs in args._get_kwargs()])
log.info('{:.1f}s\targs: {}'.format(time()-start,tmpstr))
tmpstr  = ' , '.join([key+'='+str(fdict[key]) for key in fdict.keys()])
log.info('{:.1f}s\tfdict: {}'.format(time()-start,tmpstr))



# AR copied from make_mtl()
mtldatamodel = np.array([], dtype=[
    ('RA', '>f8'), ('DEC', '>f8'), ('PARALLAX', '>f4'),
    ('PMRA', '>f4'), ('PMDEC', '>f4'), ('REF_EPOCH', '>f4'),
    ('DESI_TARGET', '>i8'), ('BGS_TARGET', '>i8'), ('MWS_TARGET', '>i8'),
    ('SCND_TARGET', '>i8'), ('TARGETID', '>i8'),
    ('SUBPRIORITY', '>f8'), ('OBSCONDITIONS', 'i4'),
    ('PRIORITY_INIT', '>i8'), ('NUMOBS_INIT', '>i8'), ('PRIORITY', '>i8'),
    ('NUMOBS', '>i8'), ('NUMOBS_MORE', '>i8'), ('Z', '>f8'), ('ZWARN', '>i8'),
    ('TIMESTAMP', 'S19'), ('VERSION', 'S14'), ('TARGET_STATE', 'S15')
    ])



# AR extra-hdu for dithering
extradatamodel = np.array([], dtype=[
	('UNDITHER_RA','>f8'),('UNDITHER_DEC','>f8'),('TARGETID', '>i8')
	])



# AR obscon
if   (args.obscon=='dark,bright'): obscon = 'DARK|GRAY|BRIGHT'
elif (args.obscon=='dark'):        obscon = 'DARK|GRAY'
else:                              obscon = 'BRIGHT' # AR bright
log.info('{:.1f}s\tsetting obscon = {}'.format(time()-start,obscon))



# AR get matching index for two np arrays, those should be arrays with unique values, like id
# AR https://stackoverflow.com/questions/32653441/find-indices-of-common-values-in-two-arrays
# AR we get: A[maskA] = B[maskB]
def unq_searchsorted(A,B):
	# AR sorting A,B
	tmpA  = np.sort(A)
	tmpB  = np.sort(B)
	# AR create mask equivalent to np.in1d(A,B) and np.in1d(B,A) for unique elements
	maskA = (np.searchsorted(tmpB,tmpA,'right') - np.searchsorted(tmpB,tmpA,'left'))==1
	maskB = (np.searchsorted(tmpA,tmpB,'right') - np.searchsorted(tmpA,tmpB,'left'))==1
	# AR to get back to original indexes
	return np.argsort(A)[maskA],np.argsort(B)[maskB]



# AR ! not using make_mtl !
# AR for commissioning, Adam says we should not use make_mtl, assign mtl columns by hand [email Oct, 17 2020]
# AR by default, we propagate {PRIORITY,NUMOBS}_INIT to {PRIORITY,NUMOBS_MORE}
# AR mtl (reproducing steps of make_mtl())
def cmx_make_mtl(d,outfn):
	# d     : output of read_targets_in_tiles()
	# outfn : written fits file
	mtl                = Table(d)
	mtl.meta['EXTNAME']= 'MTL'
	for col in ['NUMOBS_MORE', 'NUMOBS', 'Z', 'ZWARN', 'TARGET_STATE', 'TIMESTAMP', 'VERSION']:
		mtl[col] = np.empty(len(mtl), dtype=mtldatamodel[col].dtype)
	mtl['NUMOBS_MORE'] = mtl['NUMOBS_INIT']
	mtl['PRIORITY']    = mtl['PRIORITY_INIT']
	mtl['TARGET_STATE']= 'UNOBS'
	mtl['TIMESTAMP']   = datetime.utcnow().isoformat(timespec='seconds')
	mtl['VERSION']     = '-1'			# AR : TBD : to be updated when pushed in desihub/fiberassign
	obsconmask = set_obsconditions(d)	# AR : TBD : do we want to set obsconmask to 1? (see Ted s email)
	mtl['OBSCONDITIONS'] = obsconmask
	n,tmpfn = write_mtl(args.outdir,mtl.as_array(),indir=args.outdir,survey='cmx',ecsv=False)
	os.rename(tmpfn,outfn)
	log.info('{:.1f}s\tmtl targets written to {} , moved to {}'.format(time()-start,tmpfn,outfn)) 
	return True



# AR tiles
if (dotile==True):
	hdr = fitsio.FITSHDR()
	for tileid in tileids:
		d   = np.zeros(1, dtype=[
				('TILEID','i4'),
				('RA','f8'),('DEC','f8'),
				('OBSCONDITIONS','i4'),
				('IN_DESI','i2'),
				('PROGRAM', 'S6')])
		d['TILEID']  = tileid
		d['RA']      = args.tilera
		d['DEC']     = args.tiledec
		d['IN_DESI'] = is_point_in_desi(dmio.load_tiles(),args.tilera,args.tiledec).astype(int)
		d['PROGRAM'] = 'CMX' # AR custom...
		d['OBSCONDITIONS'] = obsconditions.mask(obscon) # AR we force the obsconditions to args.obscon
		fitsio.write('{}{:06d}-tiles.fits'.format(args.outdir,tileid),d,extname='TILES',header=hdr,clobber=True)  
		log.info('{:.1f}s\t{}{:06d}-tiles.fits written'.format(time()-start,args.outdir,tileid))



# AR sky
if (dosky==True):
	tiles    = fits.open('{}-tiles.fits'.format(root))[1].data
	d        = read_targets_in_tiles(mydirs['sky'],    tiles=tiles)
	dsupp    = read_targets_in_tiles(mydirs['skysupp'],tiles=tiles)
	n,tmpfn  = write_targets(args.outdir,np.concatenate([d,dsupp]),
					indir=mydirs['sky'],indir2=mydirs['skysupp'],survey='cmx')
	os.rename(tmpfn,'{}-sky.fits'.format(root))
	log.info('{:.1f}s\t{}-sky.fits written'.format(time()-start,root))



# AR gfa
if (dogfa==True):
	tiles    = fits.open('{}-tiles.fits'.format(root))[1].data
	d        = read_targets_in_tiles(mydirs['gfa'],tiles=tiles)
	n,tmpfn  = write_targets(args.outdir,d,indir=mydirs['gfa'],survey='cmx')
	os.rename(tmpfn,'{}-gfa.fits'.format(root))
	log.info('{:.1f}s\t{}-gfa.fits written'.format(time()-start,root))



# AR std (if flavor=science)
if (dostd==True):
	if (args.flavor=='science'):
		tiles    = fits.open('{}-tiles.fits'.format(root))[1].data
		d        = read_targets_in_tiles(mydirs['targ'],tiles=tiles,header=False)
		if   (obscon=='DARK|GRAY|BRIGHT'): std_msks = ['SV0_WD','STD_FAINT','STD_BRIGHT']
		elif (obscon=='DARK|GRAY'):        std_msks = ['SV0_WD','STD_FAINT']
		elif (obscon=='BRIGHT'):           std_msks = ['SV0_WD','STD_BRIGHT']
		else:
			log.error('{:.1f}s\tobson not in DARK|GRAY|BRIGHT,DARK|GRAY,BRIGHT; exiting'.format(time()-start))
			sys.exit()
		keep     = np.zeros(len(d),dtype=bool)
		for msk in std_msks:
			keep |= ((d['CMX_TARGET'] & cmx_mask[msk])>0)
			log.info('{:.1f}s\tkeeping {:.0f} {} stds'.format(time()-start,((d['CMX_TARGET'] & cmx_mask[msk])>0).sum(),msk))
		# AR removing overlap with science targets
		isscience = np.zeros(len(d),dtype=bool)
		for msk in fdict['msks'].split(','):
			isscience |= ((d['CMX_TARGET'] & cmx_mask[msk])>0)
		keep[isscience] = False
		d        = d[keep]
		log.info('{:.1f}s\tkeeping {:.0f}/{:.0f} stds after having cut on {} and removed {}'.format(time()-start,keep.sum(),len(keep),std_msks,fdict['msks']))
		# AR custom mtl
		_ = cmx_make_mtl(d,'{}-std.fits'.format(root))



# AR (undithered) targets
# AR ! not using make_mtl !
if (dotarg==True):
	tiles    = fits.open('{}-tiles.fits'.format(root))[1].data
	d,hdr    = read_targets_in_tiles(mydirs['targ'],tiles=tiles,header=True)
	keep     = np.zeros(len(d),dtype=bool)
	for msk in fdict['msks'].split(','):
		keep |= ((d['CMX_TARGET'] & cmx_mask[msk])>0)
		log.info('{:.1f}s\tkeeping {:.0f} {} targets'.format(time()-start,((d['CMX_TARGET'] & cmx_mask[msk])>0).sum(),msk))
	d        = d[keep]
	log.info('{:.1f}s\tkeeping {:.0f}/{:.0f} targets after having cut on {}'.format(time()-start,keep.sum(),len(keep),fdict['msks']))
	# AR DITHER : tweaking PRIORITY and NUMOBS_MORE + updating the header
	if (args.flavor in ['dithprec','dithlost']):
		d['PRIORITY_INIT'] = 1210-np.clip(d['GAIA_PHOT_RP_MEAN_MAG']*10, 100, 210).astype('i4') 
		d['NUMOBS_INIT']   = 1
		log.info('{:.1f}s\tPRIORITY_INIT and NUMOBS_INIT tweaked for dithering'.format(time()-start))
	# AR custom mtl
	_ = cmx_make_mtl(d,'{}-targ.fits'.format(root))
	# AR DITHER: update header
	if (args.flavor in ['dithprec','dithlost']):
		fd  = fitsio.FITS('{}-targ.fits'.format(root),'rw')
		fd['MTL'].write_key('COMMENT','tweak : PRIORITY_INIT = 1210-np.clip(GAIA_PHOT_RP_MEAN_MAG*10,100,210)')
		fd['MTL'].write_key('COMMENT','tweak : NUMOBS_INIT = 1')
		fd.close()


# AR fiberassign
if (dofa==True):
	for tileid in tileids:
		# AR first case:  undithered -> after  running fiberassign, we get the ras, decs, and the indexes to be dithered
		# AR other cases: dithered-??-> before running fiberassign, we compute/apply the dithering offsets
		troot = '{}{:06d}'.format(args.outdir,tileid)
		#
		if ((args.flavor in ['dithprec','dithlost']) & (tileid!=tileids[0])):
			#
			raoffs,decoffs   = ras.copy(),decs.copy()
			# AR Gaussian offset computation
			if (len(ginds)>0):
				raoffs [ginds]  += np.random.randn(len(ginds))*fdict['gwidth']/3600. / np.cos(np.radians(decs[ginds]))
				decoffs[ginds]  += np.random.randn(len(ginds))*fdict['gwidth']/3600.
			# AR dithlost-in-space offset within a box
			if (len(linds)>0):
				raoffs [linds]  += (1-2*np.random.rand(len(linds)))*fdict['bwidth']/2./3600. / np.cos(np.radians(decs[linds]))
				decoffs[linds]  += (1-2*np.random.rand(len(linds)))*fdict['bwidth']/2./3600.
			# AR updating ra,dec + cutting on dithered targets + updating header + writing
			h                = fits.open(root+'-targ.fits')
			h[1].data['RA']  = raoffs
			h[1].data['DEC'] = decoffs
			# AR cutting on dithered targets
			h[1].data        = h[1].data[np.sort(ginds+linds)]
			# AR adding infos in the header
			for kwargs in args._get_kwargs():
				h[1].header[kwargs[0]] = kwargs[1]
			for key in fdict.keys():
				h[1].header[key] = str(fdict[key])
			h.writeto(troot+'-targ.fits',overwrite=True)
		# AR running fiberassign
		if (args.flavor=='science'):
			opts = ['--targets',   troot+'-targ.fits',root+'-std.fits',]
		else:
			opts = ['--targets',   troot+'-targ.fits',]
		opts+= [
				'--rundate',   args.rundate,
				'--overwrite',
				'--write_all_targets',
				'--footprint',     troot+'-tiles.fits',
				'--dir',           args.outdir,
				'--sky',           root+'-sky.fits',
				'--sky_per_petal', fdict['nskypet'],
				'--gfafile',       root+'-gfa.fits',
				]
		log.info('{:.1f}s\ttileid={:06d}: running raw fiber assignment (fba_run) with opts={}'.format(time()-start,tileid,' ; '.join(opts)))
		ag = parse_assign(opts)
		run_assign_full(ag)
		# AR merging
		opts = [
				'--skip_raw',
				'--dir',           args.outdir,
				'--targets',       troot+'-targ.fits',root+'-sky.fits',root+'-gfa.fits'
				]
		log.info('{:.1f}s\ttileid={:06d}: merging input target data (fba_merge_results) with opts={}'.format(time()-start,tileid,' ; '.join(opts)))
		ag = parse_merge(opts)
		run_merge(ag)
		# AR dither flavors: temporary moving fba-{tileid}.fits file, otherwise it confuses run_merge()
		if (args.flavor in ['dithprec','dithlost']):
			fn       = '{}fba-{:06d}.fits'.format(args.outdir,tileid)
			os.rename(fn,fn.replace('.fits','-tmp.fits'))
			log.info('{:.1f}s\trenaming {} to {}'.format(time()-start,fn,fn.replace('.fits','-tmp.fits')))
		# AR propagating some settings into the PRIMARY header
		fd  = fitsio.FITS('{}fiberassign-{:06d}.fits'.format(args.outdir,tileid), 'rw')
		for key in np.sort(list(mydirs.keys())):
			fd['PRIMARY'].write_key(key,mydirs[key])
		for kwargs in args._get_kwargs():
			if (kwargs[0].lower() in ['outdir','intileid','flavor','rundate','obscon','seed']):
				if (kwargs[1] is not None): 
					fd['PRIMARY'].write_key(kwargs[0],kwargs[1])
		fd.close()
		# AR adding an extra-hdu for the dithering
		# AR ~copied from https://github.com/desihub/fiberassign/blob/52cb99424d8a1d4e5366e6a200636ab02cb71bb9/py/fiberassign/assign.py#L1141-L1208
		if ((args.flavor in ['dithprec','dithlost']) & (tileid!=tileids[0])):
			dithfn   = '{}fiberassign-{:06d}.fits'.format(args.outdir,tileid)
			undithfn = '{}{:06d}-targ.fits'.format(args.outdir,tileids[0])
			tmpfn    = dithfn.replace('.fits','-tmp.fits')
			if os.path.isfile(tmpfn):
				os.remove(tmpfn)
			fdin     = fitsio.FITS(dithfn,'r')
			fd       = fitsio.FITS(tmpfn,'rw')
			# AR copying troot+'-fiberassign.fits'
			extnames = ['PRIMARY','FIBERASSIGN','SKY_MONITOR','GFA_TARGETS','TARGETS','POTENTIAL_ASSIGNMENTS']
			for iext,extname in enumerate(extnames):
				if (iext!=fdin[extname].get_extnum()):
					log.error('{:.1f}s\t{}}-fiberassign.fits extensions not ordered as expected ({}); exiting'.format(time()-start),troot,','.join(extnames))
					sys.exit()
				if (extname=='PRIMARY'):
					fd.write(None, header=fdin[extname].read_header(), extname=extname)
				else:
					fd.write(fdin[extname].read(), header=fdin[extname].read_header(), extname=extname)
			# AR extra-hdu with UNDITHERED_RA, UNDITHERED_DEC
			# AR reading *-fiberassign.fits, and updating TARGET_RA,TARGET_DEC
			# AR with the undithered positions for TARGETID matched with root+'-targ.fits'
			d                   = fits.open(dithfn  )[1].data
			dundith             = fits.open(undithfn)[1].data
			ii,iiundith         = unq_searchsorted(d['TARGETID'],dundith['targetid'])
			d['TARGET_RA'] [ii] = dundith['RA'] [iiundith]
			d['TARGET_DEC'][ii] = dundith['DEC'][iiundith]
			dextra  = Table()
			for key in extradatamodel.dtype.names:
				dextra[key] = np.empty(len(d), dtype=extradatamodel[key].dtype)
			dextra['TARGETID']    = d['TARGETID']
			dextra['UNDITHER_RA'] = d['TARGET_RA']
			dextra['UNDITHER_DEC']= d['TARGET_DEC']
			hdr0 = fdin[0].read_header()
			hdr  = {}
			for key in hdr0.keys():
				if (key not in ['SIMPLE', 'BITPIX', 'NAXIS', 'EXTEND', 'COMMENT', 'EXTNAME']):
					hdr[key] = hdr0[key]
			hdr['UNDITHFN'] = '{}fiberassign-{:06d}.fits'.format(args.outdir,tileids[0])
			fd.write(dextra.as_array(), header=hdr, extname='EXTRA')
			fd.close()
			# AR renaming
			os.rename(tmpfn,dithfn)
			log.info('{:.1f}s\t{}: additional EXTRA extension added'.format(time()-start,dithfn))
		# AR identifiying assigned targets (=STD_DITHER) on the undithered tile
		if ((args.flavor in ['dithprec','dithlost']) & (tileid==tileids[0])):
			d     = fits.open('{}fiberassign-{:06d}.fits'.format(args.outdir,tileid))[1].data
			# AR removing sky fibres
			tids  = d['TARGETID'][d['OBJTYPE']=='TGT']
			log.info('{:.1f}s\t{}: {:.0f} {} assigned'.format(time()-start,troot,len(tids),fdict['msks']))
			# AR
			h       = fits.open(root+'-targ.fits')
			ras,decs= h[1].data['RA'],h[1].data['DEC']
			# AR targets to be offset
			inds    = np.where(np.in1d(h[1].data['TARGETID'],tids))[0]				# AR indexes of assigned targets
			if (fdict['gfrac']>0):													# AR targets to be offset by a Gaussian
				ginds   = np.random.choice(inds,size=int(fdict['gfrac']*len(inds)),replace=False).tolist()
			else:
				ginds   = []
			# AR targets to be offset within a box (dithlost-in-space)
			tmpinds = inds[~np.in1d(inds,ginds)]									# AR targets not offset by a Gaussian
			if (fdict['bfrac']>0):
				if (fdict['gfrac']+fdict['bfrac']==1):	tmpn = len(inds) - len(ginds)
				else:									tmpn = int(fdict['bfrac']*len(inds))
				linds   = np.random.choice(tmpinds,size=tmpn,replace=False).tolist()# AR targets to be offset within a box
			else:
				linds   = []
	# AR dither flavors: re-naming fba-{tileid}.fits files
	if (args.flavor in ['dithprec','dithlost']):
		for tileid in tileids:
			fn       = '{}fba-{:06d}.fits'.format(args.outdir,tileid)
			os.rename(fn.replace('.fits','-tmp.fits'),fn)
			log.info('{:.1f}s\trenaming {} to {}'.format(time()-start,fn.replace('.fits','-tmp.fits'),fn))




if (doplot==True):

	# AR https://lmfit.github.io/lmfit-py/builtin_models.html#lmfit.models.GaussianModel
	def gaussian(x, amp, cen, wid):
		return amp/(wid*np.sqrt(2*np.pi)) * np.exp(-(x-cen)**2 /wid)

	def mycmap(name,n,cmin,cmax):
		cmaporig = matplotlib.cm.get_cmap(name)
		mycol    = cmaporig(np.linspace(cmin, cmax, n))
		cmap     = matplotlib.colors.ListedColormap(mycol)
		cmap.set_under(mycol[0])
		cmap.set_over (mycol[-1])
		return cmap
	
	cm       = mycmap('jet_r',10,0,1)

	def plot_hist(ax,x,xp,bins,xlabel):
		# x : x-quantity for the assigned sample
		# xp: x-quantity for the parent sample
		cps,_,_= ax.hist(xp,bins=bins,histtype='step',alpha=0.3,lw=3,  color='k',density=False,label='parent')
		cs,_,_,= ax.hist(x, bins=bins,histtype='step',alpha=1.0,lw=1.0,color='k',density=False,label='assigned')
		ax.set_xlabel(xlabel)
		ax.set_ylabel('counts')
		ax.grid(True)
		ax.legend(loc=2)
		axr   = ax.twinx()
		axr.plot(0.5*(bins[1:]+bins[:-1]),np.array(cs)/np.array(cps).astype(float),color='r',lw=0.5)
		axr.yaxis.label.set_color('r')
		axr.tick_params(axis='y',colors='r')
		axr.set_ylabel('ratio',labelpad=-10)
		axr.set_ylim(0,1)
		return

	# AR tile ra,dec
	tiles    = fits.open(root+'-tiles.fits')[1].data
	tra,tdec = tiles['RA'][0],tiles['DEC'][0]
	tsky     = SkyCoord(ra=tra*units.deg,dec=tdec*units.deg,frame='icrs')

	# AR control plots
	# AR parent
	dp     = fits.open(root+'-targ.fits')[1].data
	skyp   = SkyCoord(ra=dp['RA']*units.deg,dec=dp['DEC']*units.deg,frame='icrs')
	#
	for tileid in tileids:
		d      = fits.open('{}fiberassign-{:06d}.fits'.format(args.outdir,tileid))[1].data
		mydict = {}
		for key in ['SKY','BAD','TGT']:
			mydict['N'+key] = (d['OBJTYPE']==key).sum()
		keys   = [	'TARGETID','PETAL_LOC','CMX_TARGET','FLUX_G','FLUX_R','FLUX_Z',
					'TARGET_RA','TARGET_DEC','GAIA_PHOT_RP_MEAN_MAG','PRIORITY']
		# AR arrays following the parent ordering
		d      = d[d['OBJTYPE']=='TGT']
		iip,ii = unq_searchsorted(dp['TARGETID'],d['TARGETID'])
		for key in keys:
			if (key=='CMX_TARGET'):
				mydict[key] = np.zeros(len(dp),dtype=int)
			else:
				mydict[key] = np.nan + np.zeros(len(dp))
			mydict[key][iip] =  d[key][ii]
		sky   = SkyCoord(ra=mydict['TARGET_RA']*units.deg,dec=mydict['TARGET_DEC']*units.deg,frame='icrs')
		#
		fig   = plt.figure(figsize=(25,15))
		title = 'flavor={}    TILEID={:06d} at RA,DEC={:.1f},{:.1f}   obscon={}\n'.format(args.flavor,tileid,tra,tdec,obscon)
		title+= 'SKY={:.0f} , BAD={:.0f} , TGT={:.0f} ('.format(mydict['NSKY'],mydict['NBAD'],mydict['NTGT'])
		title+= ' , '.join(['{}={:.0f}'.format(msk,((mydict['CMX_TARGET'] & cmx_mask[msk])>0).sum()) for msk in fdict['msks'].split(',')])
		title+= ')'			
		fig.text(0.5,0.9,title,ha='center',fontsize=15,transform=fig.transFigure)
		gs    = gridspec.GridSpec(3,4,wspace=0.3,hspace=0.2)

		# AR grz-mags
		for ip,key in enumerate(['FLUX_G','FLUX_R','FLUX_Z']):
			ax   = plt.subplot(gs[0,ip])
			keep = (dp[key]>0)
			xp   = 22.5-2.5*np.log10(dp[key][keep])
			bitp = dp['CMX_TARGET'][keep]
			keep = (mydict[key]>0)
			x    = 22.5-2.5*np.log10(mydict[key][keep])
			bit  = mydict['CMX_TARGET'][keep]
			bins = np.linspace(xp.min(),xp.max(),51)
			plot_hist(ax,x,xp,bins,'22.5 - 2.5 * log10({})'.format(key))
			_,ymax = ax.get_ylim()
			ax.set_ylim(0.8,100*ymax)
			ax.set_yscale('log')

		# AR grz-diagram 
		ax   = plt.subplot(gs[0,3])
		grp  = -2.5*np.log10(dp['FLUX_G']/dp['FLUX_R'])
		rzp  = -2.5*np.log10(dp['FLUX_R']/dp['FLUX_Z'])
		gr   = -2.5*np.log10(mydict['FLUX_G']/mydict['FLUX_R'])
		rz   =  -2.5*np.log10(mydict['FLUX_R']/mydict['FLUX_Z'])
		ax.scatter(rzp,grp,c='k',s=2,alpha=0.1,rasterized=True,label='parent')
		ax.scatter(rz, gr, c='r',s=2,alpha=1.0,rasterized=True,label='assigned')
		ax.set_xlabel('-2.5 * log10(FLUX_R / FLUX_Z)')
		ax.set_ylabel('-2.5 * log10(FLUX_G / FLUX_R)')
		ax.set_xlim(-0.5,2.5)
		ax.set_ylim(-0.5,2.5)
		ax.grid(True)
		ax.legend(loc=4)



		# AR position in tile
		ax                 = plt.subplot(gs[1,0]) # AR will be over-written
		xlim,ylim,gridsize = (2,-2),(-2,2),50
		plot_area          = (xlim[0]-xlim[1]) * (ylim[1]-ylim[0]) # AR area of the plotting window in deg2
		# AR parent
		spho = tsky.spherical_offsets_to(skyp) # AR in degrees
		drap = spho[0].value
		ddecp= spho[1].value
		hbp  = ax.hexbin(drap,ddecp,C=None,gridsize=gridsize,extent=(xlim[1],xlim[0],ylim[0],ylim[1]),mincnt=0,visible=False)
		# AR assigned
		spho = tsky.spherical_offsets_to(sky) # AR in degrees
		dra = spho[0].value
		ddec= spho[1].value
		hb   = ax.hexbin(dra,ddec,C=None,gridsize=gridsize,extent=(xlim[1],xlim[0],ylim[0],ylim[1]),mincnt=0,visible=False)
		# 
		tmpx = hb.get_offsets()[:,0]
		tmpy = hb.get_offsets()[:,1]
		keep = (hbp.get_array()>0)
		carea= plot_area / len(hbp.get_array())	# AR plt.hexbin "cell" area
		area = carea * keep.sum()				# AR ~desi fov area in deg2
		#
		for ip,c,clab in zip(
				[0,1,2],
				[hbp.get_array()/carea,hb.get_array()/len(tileids)/carea,(hb.get_array()/hbp.get_array())/len(tileids)],
				[r'parent density [deg$^{-2}$]',r'assigned density [deg$^{-2}$]','parent fraction assigned']):
			cmin = c[keep].mean()-3*c[keep].std()
			cmin = np.max([0,cmin])
			cmax = c[keep].mean()+3*c[keep].std()
			if (ip==2):
				cmax = np.min([1,cmax])
				txt  = r'mean = {:.2f}'.format(c[keep].mean())
			else:
				txt  = r'mean = {:.0f}'.format(c[keep].sum()*carea/area)+' deg$^{-2}$'
			ax  = plt.subplot(gs[1,ip])
			SC  = ax.scatter(tmpx[keep],tmpy[keep],c=c[keep],s=15,vmin=cmin,vmax=cmax,alpha=0.5,cmap=cm)
			ax.set_xlabel(r'$\Delta$RA = Angular distance to TILE_RA [deg.]')
			ax.set_ylabel(r'$\Delta$DEC = Angular distance to TILE_DEC [deg.]')
			ax.set_xlim(xlim)
			ax.set_ylim(ylim)
			ax.grid(True)
			ax.text(0.02,0.93,txt,color='k',fontweight='bold',fontsize=15,transform=ax.transAxes)
			cbar = plt.colorbar(SC)
			cbar.set_label(clab)
			cbar.mappable.set_clim(cmin,cmax)

		# AR dithering positions
		if (args.flavor in ['dithprec','dithlost']):
			xmax    = 5*fdict['gwidth']
			if (fdict['bfrac']>0):
				xmax = np.max([xmax,1.5*fdict['bwidth']/2.])
			# AR positions
			spho    = skyp.spherical_offsets_to(sky)	# AR in degrees
			dra     = spho[0].value.flatten() * 3600.	# AR in arcsec
			ddec    = spho[1].value.flatten() * 3600.	# AR in arcsec
			keep    = (np.isfinite(dra)) & (np.isfinite(ddec))
			dra,ddec= dra[keep],ddec[keep]
			ax      = plt.subplot(gs[2,0])
			ax.scatter(dra,ddec,c='k',s=5,alpha=0.2)
			axx     = ax.twinx()
			axx.hist(dra, bins=100,histtype='stepfilled',alpha=0.3,color='k',density=True)
			axx.set_ylim(0,5)
			axx.axis('off')
			axy     = ax.twiny()
			axy.hist(ddec,bins=100,histtype='stepfilled',alpha=0.3,color='k',density=True,orientation='horizontal')
			axy.set_xlim(0,5)
			axy.axis('off')
			ax.set_xlabel('$\Delta$RA = Angular offset in R.A. [arcsec]')
			ax.set_ylabel('$\Delta$DEC = Angular offset in Dec. [arcsec]')
			ax.set_xlim(-xmax,xmax)
			ax.set_ylim(-xmax,xmax)
			ax.grid(True)
			txt     = r'$\Delta$RA ={:.3f}$\pm${:.3f} arcsec'.format(dra.mean(),dra.std())
			ax.text(0.02,0.93,txt,color='k',fontweight='bold',fontsize=10,transform=ax.transAxes)
			txt     = r'$\Delta$DEC={:.3f}$\pm${:.3f} arcsec'.format(ddec.mean(),ddec.std())
			ax.text(0.02,0.89,txt,color='k',fontweight='bold',fontsize=10,transform=ax.transAxes)
			# AR gaussian / box
			if (fdict['gfrac']>0):
				tmpx    = np.linspace(-xmax,xmax,1000)
				tmpy    = gaussian(tmpx,1.0,0.0,fdict['gwidth'])
				axx.plot(tmpx,tmpy,color='k',label='Gaussian(0,'+'%.2f'%fdict['gwidth']+')')
				axy.plot(tmpy,tmpx,color='k')
				axx.legend(loc=1)
			if (fdict['bfrac']>0):
				ax.axhline(+fdict['bwidth']/2.,ls='--',color='k',label='box of '+'%.2f'%fdict['bwidth']+' width')
				ax.axhline(-fdict['bwidth']/2.,ls='--',color='k')
				ax.axvline(+fdict['bwidth']/2.,ls='--',color='k')
				ax.axvline(-fdict['bwidth']/2.,ls='--',color='k')
				ax.legend(loc=4)

		# AR rmag
		ax   = plt.subplot(gs[2,1])
		xp   = dp['GAIA_PHOT_RP_MEAN_MAG']
		x    = mydict['GAIA_PHOT_RP_MEAN_MAG']
		x    = x[np.isfinite(x)]
		bins = np.linspace(xp.min(),xp.max(),51)
		plot_hist(ax,x,xp,bins,'GAIA_PHOT_RP_MEAN_MAG')

		# AR priority
		ax   = plt.subplot(gs[2,2])
		xp   = dp['PRIORITY']
		x    = mydict['PRIORITY']
		bins = np.linspace(xp.min(),xp.max(),xp.max()-xp.min()+1)
		plot_hist(ax,x,xp,bins,'PRIORITY')

		#  AR saving plot
		plt.savefig('{}fiberassign-{:06d}.png'.format(args.outdir,tileid),bbox_inches='tight')
		plt.close()


# AR do clean?
if (args.doclean=='y'):
	for tileid in tileids:
		for ext in ['tiles','sky','gfa','std','targ']:
			fn  = '{}{:06d}-{}.fits'.format(args.outdir,tileid,ext)
			if (os.path.isfile(fn)):
				log.info('{:.1f}s\tremoving {}'.format(time()-start,fn))
				os.remove(fn)


