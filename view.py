import os
import time
import json
import glob
import cPickle
import numpy as np
from scipy.interpolate import interp1d

import db

cwd = os.path.split(os.path.abspath(__file__))[0]
options = json.load(open(os.path.join(cwd, "defaults.json")))

def _gen_flat_mask(pts, polys, height=1024):
    import polyutils
    import Image
    import ImageDraw
    pts = pts.copy()
    pts -= pts.min(0)
    pts *= height / pts.max(0)[1]
    im = Image.new('L', pts.max(0), 0)
    draw = ImageDraw.Draw(im)

    left, right = polyutils.trace_both(pts, polys)
    draw.polygon(pts[left], outline=None, fill=255)
    draw.polygon(pts[right], outline=None, fill=255)
    
    del draw
    return np.array(im) > 0

def _make_flat_cache(interp, xfm, height=1024):
    from scipy.interpolate import griddata
    fiducial, flat = interp(0), interp(1)
    wpts = np.append(fiducial, np.ones((len(fiducial), 1)), axis=-1).T
    coords = np.dot(xfm, wpts)[:3].T
    fmax, fmin = flat.max(0), flat.min(0)
    size = fmax - fmin
    aspect = size[0] / size[-1]
    width = aspect * 1024

    flatpos = np.mgrid[fmin[0]:fmax[0]:width*1j, fmin[-1]:fmax[-1]:height*1j].reshape(2,-1)
    pcoords = griddata(flat[:,[0,2]], coords, flatpos.T, method="nearest")
    return pcoords, (width, height)

def _get_surf_interp(subject, types=('inflated',), hemisphere="both"):
    types = ("fiducial",) + types + ("flat",)
    pts = []
    for t in types:
        pt, polys, norm = db.surfs.getVTK(subject, t, hemisphere=hemisphere)
        pts.append(pt)

    #flip the flats to be on the X-Z plane
    flatpts = np.zeros_like(pts[-1])
    flatpts[:,[0,2]] = pts[-1][:,:2]
    flatpts[:,1] = pts[-2].min(0)[1]
    pts[-1] = flatpts

    interp = interp1d(np.linspace(0,1,len(pts)), pts, axis=0)
    return interp, polys

def _tcoords(subject, hemisphere="both"):
    pts, polys, norm = db.surfs.getVTK(subject, "flat", hemisphere="both")
    pts = pts[:,:2] - pts[:,:2].min(0)
    pts /= pts.max(0)
    if hemisphere == "both":
        return pts
    elif hemisphere == "rh":
        h, polys, norm = db.surfs.getVTK(subject, "flat", hemisphere=hemisphere)
        return pts[-len(h):]
    elif hemisphere == "lh":
        h, polys, norm = db.surfs.getVTK(subject, "flat", hemisphere=hemisphere)
        return pts[len(h):]

def show(data, subject, xfm, types=('inflated',), hemisphere="both"):
    '''View epi data, transformed into the space given by xfm. 
    Types indicates which surfaces to add to the interpolater. Always includes fiducial and flat'''
    interp, polys = _get_surf_interp(subject, types, hemisphere)

    if hasattr(data, "get_affine"):
        #this is a nibabel file -- it has the nifti headers intact!
        if isinstance(xfm, str):
            xfm = db.surfs.getXfm(subject, xfm, xfmtype="magnet")
            assert xfm is not None, "Cannot find transform by this name!"
            xfm = np.dot(np.linalg.inv(data.get_affine()), xfm[0])
        data = data.get_data()
    elif isinstance(xfm, str):
        xfm = db.surfs.getXfm(subject, xfm, xfmtype="coord")
        assert xfm is not None, "Cannot find coord transform, please provide a nifti!"
        xfm = xfm[0]
    assert xfm.shape == (4, 4), "Not a transform matrix!"
    
    overlay = os.path.join(options['file_store'], "overlays", "%s_rois.svg"%subject)
    if not os.path.exists(overlay):
        #Can't find the roi overlay, create a new one!
        pts = interp(1)
        size = pts.max(0) - pts.min(0)
        aspect = size[0] / size[-1]
        with open(overlay, "w") as xml:
            xmlbase = open(os.path.join(cwd, "svgbase.xml")).read()
            xml.write(xmlbase.format(width=aspect * 1024, height=1024))
    
    kwargs = dict(points=interp, polys=polys, xfm=xfm, data=data, svgfile=overlay)
    if hemisphere != "both":
        kwargs['tcoords'] = _tcoords(subject, hemisphere)

    import mixer
    m = mixer.Mixer(**kwargs)
    m.edit_traits()
    return m

def quickflat(data, subject, xfmname, recache=False, height=1024):
    cachename = "{subj}_{xfm}_{h}_*.pkl".format(subj=subject, xfm=xfmname, h=height)
    cachefile = os.path.join(options['file_store'], "flatcache", cachename)
    #pull a list of candidate cache files
    files = glob.glob(cachefile)
    if len(files) < 1 or recache:
        #if recaching, delete all existing files
        for f in files:
            os.unlink(f)
        print "Generating a flatmap cache"
        #pull points and transform from database
        interp, polys = _get_surf_interp(subject, types=())
        xfm = db.surfs.getXfm(subject, xfmname, xfmtype="coord")[0]
        #Generate the lookup coordinates and the mask
        coords, size = _make_flat_cache(interp, xfm, height=height)
        mask = _gen_flat_mask(interp(1)[:,[0,2]], polys, height=height).T
        #save them into the proper file
        date = time.strftime("%Y%m%d")
        cachename = "{subj}_{xfm}_{h}_{date}.pkl".format(
            subj=subject, xfm=xfmname, h=height, date=date)
        cachefile = os.path.join(options['file_store'], "flatcache", cachename)
        cPickle.dump((coords, size, mask), open(cachefile, "w"), 2)
    else:
        coords, size, mask = cPickle.load(open(files[0]))
    coords = coords.round()

    ravelpos = coords[:,0]*data.shape[1]*data.shape[0]
    ravelpos += coords[:,1]*data.shape[0] + coords[:,2]
    validpos = ravelpos[mask.ravel()].astype(int)
    img = np.ones_like(ravelpos)
    img *= np.nan
    img[mask.ravel()] = data.T.ravel()[validpos]
    return img.reshape(size).T[::-1]

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Display epi data on various surfaces, \
        allowing you to interpolate between the surfaces")
    parser.add_argument("epi", type=str)
    parser.add_argument("--transform", "-T", type=str)
    parser.add_argument("--surfaces", nargs="*")