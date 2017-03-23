from __future__ import print_function, division

# Import the usual libraries
import numpy as np

import pynrc
from pynrc import nrc_utils
from pynrc.nrc_utils import webbpsf, poppy

import astropy.io.fits as fits
import multiprocessing as mp

pynrc.setup_logging('WARNING', verbose=False)

from poppy import zernike
from poppy.optics import MultiHexagonAperture
from poppy.utils import pad_to_size

class OPD_extract(object):

    def __init__(self, opd, header, verbose=False):

        self.opd = opd
        self.npix = opd.shape[0]
        self.pix_scale = header['PUPLSCAL'] # pupil scale in meters/pixel

        # Create a list of segment masks
        self.nseg = 18
        # Flat to flat is 1.32 meters with a 3 mm air gain between mirrors
        # Supposedly there's a 15 mm effective gap due to polishing
        f2f = 1.308
        gap = 0.015

        # Analytic pupil mask for all segments
        # To work with the OPD Rev V masks, use npix=1016 and pad to 1024
        self._mask_pupil = MultiHexagonAperture(rings=2, flattoflat=f2f, gap=gap)

        # Individual segments in a list
        self._mask_segs = []
        for i in np.arange(self.nseg)+1:
            self._mask_segs.append(MultiHexagonAperture(rings=2, flattoflat=f2f, gap=gap, segmentlist=[i]))

        # Get the x/y positions 
        self.segment_positions = [self._get_seg_xy(i) for i in range(self.nseg)]

        self.basis_zernike = zernike.zernike_basis_faster
        self.basis_hexike  = zernike.hexike_basis

        if verbose: print('Fitting Zernike coefficients for entire pupil...')
        self._coeff_pupil = self._get_coeff_pupil()
        if verbose: print('Constructing OPD from entire pupil coefficients...')
        self.opd_new_pupil = self._get_opd_pupil()
        self.opd_diff_pupil = self.opd - self.opd_new_pupil

        if verbose: print('Fitting Hexike coefficients for each segment...')
        self._coeff_segs = self._get_coeff_segs()
        if verbose: print('Constructing OPD for each segment...')
        self.opd_new_segs = self._get_opd_new_segs()

        print('Finished.')

    @property
    def mask_pupil(self):
        outmask, _ = self._sample_mask(self._mask_pupil)
        return outmask

    @property
    def mask_opd(self):
        mask = np.ones(self.opd.shape)
        mask[self.opd==0] = 0
        return mask

    @property
    def coeff_pupil(self):
        return self._coeff_pupil

    @property
    def coeff_segs(self):
        """Hexike coefficients for a given segment index"""
        return self._coeff_segs

    def mask_seg(self, i):
        """Return a sampled subsection of the analytic segment mask"""
        # Mask off everything but the segment of interest
        pmask = self._mask_segs[i]
        outmask, _ = self._sample_mask(pmask)
        # Multiply by larger OPD pupil mask
        outmask *= self.mask_opd

        return self.opd_seg(i,outmask)

    def opd_seg(self, i, opd_pupil=None):
        """Return a subsection of some OPD image for the provided segment index"""
        if opd_pupil is None: opd_pupil = self.opd_diff_pupil

        # Subsection the image, then pad to equal xy size
        x1,x2,y1,y2 = self.segment_positions[i]
        imsub = opd_pupil[y1:y2,x1:x2]

        (ny,nx) = imsub.shape
        npix_max = np.max(imsub.shape)

        # Make x and y the same size
        imsub = pad_to_size(imsub, [npix_max,npix_max])

        # If nx was larger than ny, then center y
        if nx > ny: # xhift in y
            shift = (ny-nx)//2 if y1>0 else (nx-ny)//2
            imsub = np.roll(imsub, shift, axis=0)
        elif ny > nx: # shift in x
            shift = (nx-ny)//2 if x1>0 else (ny-nx)//2
            imsub = np.roll(imsub, shift, axis=1)

        return imsub

    def _get_coeff_pupil(self):
        """Calculate Zernike coefficients for full OPD"""
        return self._get_coeff(self.opd, self.mask_opd, self.basis_zernike, iterations=2)

    def _get_opd_pupil(self):
        """Generate OPD for entire pupil based on coefficients"""
        opd = self._opd_from_coeff(self.coeff_pupil, self.basis_zernike, self.mask_pupil)
        return opd * self.mask_opd

    def _get_coeff_segs(self, opd_pupil=None):
        """Calculate Hexike coeffiencts each individual segment"""
        coeff_list = []
        for i in range(self.nseg): #, pmask in enumerate(self._mask_segs):
            mask_sub = self.mask_seg(i)
            opd_sub  = self.opd_seg(i, opd_pupil)
    
            coeff = self._get_coeff(opd_sub, mask_sub, self.basis_hexike, nterms=30)
            coeff_list.append(coeff)
        return coeff_list

    def _get_opd_new_segs(self, coeff_segs=None):
        """Generate segment OPDs in a list"""
        if coeff_segs is None: coeff_segs = self.coeff_segs

        opd_list = []
        for i, pmask in enumerate(self._mask_segs):
            mask_sub = self.mask_seg(i)

            opd = self._opd_from_coeff(coeff_segs[i], self.basis_hexike, mask_sub)
            opd *= mask_sub
            opd_list.append(opd)
    
        return opd_list

    def combine_opd_segs(self, opd_segs=None):
        """Combine list of OPD segments into a single image"""
        if opd_segs is None: opd_segs = self.opd_new_segs

        opd = np.zeros(self.opd.shape)
        for i, opd_sub in enumerate(opd_segs):
            pmask = self._mask_segs[i]
            mask, _ = self._sample_mask(pmask)
            mask *= self.mask_opd

            opd[mask==1] = opd_sub[opd_sub!=0]
    
        return opd

    def _get_coeff(self, opd, mask, basis, **kwargs):
        return zernike.opd_expand_nonorthonormal(opd, mask, basis=basis, **kwargs)

    def _opd_from_coeff(self, coeff, basis, mask):
        """Generate OPD image from a set of coefficients, basis function, and mask"""
        npix = mask.shape[0]
        return zernike.opd_from_zernikes(coeff, basis=basis, npix=npix, outside=0)
    
    def _sample_mask(self, mask):
        """Sample an analytic pupil mask at 1024x1024"""
        npix = self.npix
        outmask, pixelscale = mask.sample(npix=npix-8, return_scale=True)
        outmask = pad_to_size(outmask, [npix,npix])

        return outmask, pixelscale

    def _get_seg_xy(self, i):
        """
        Get the xy pixel indices (range) of a particular segment mask.
        Returns (x1,x2,y1,y2)
        """
        pmask = self._mask_segs[i]
        outmask, pixelscale = self._sample_mask(pmask)

        pix_rad = np.int(np.ceil(((pmask.side+pmask.gap) / pixelscale).value))
        pix_cen = pmask._hex_center(i+1)
        xc = pix_cen[1] / pixelscale.value + outmask.shape[1] / 2
        yc = pix_cen[0] / pixelscale.value + outmask.shape[0] / 2

        # Grab the pixel ranges
        x1 = np.int(xc - pix_rad)
        x2 = x1 + 2*pix_rad
        y1 = np.int(yc - pix_rad)
        y2 = y1 + 2*pix_rad

        # Limits on x/y positions
        x1 = np.max([x1,0])
        x2 = np.min([x2,self.npix])
        y1 = np.max([y1,0])
        y2 = np.min([y2,self.npix])

        return (x1,x2,y1,y2)

def opd_extract_helper(args):
    return OPD_extract(args[0], args[1], verbose=False)

def opd_extract_mp(opds, header, nproc=None):
    if nproc is None:
        nopd = opds.shape[0]
        nproc = int(np.min([nopd,mp.cpu_count()*0.75]))

    pool = mp.Pool(nproc)
    worker_arguments = [(opds[i,:,:],header) for i in range(nopd)]
    opds_all = pool.map(opd_extract_helper, worker_arguments)
    pool.close()
    pool.join()

    return opds_all

def opd_sci_gen(opd):
    """Function to go through an OPD class and generate a science OPD image"""
    mask_pupil = opd.mask_pupil * opd.mask_opd
    opd_pupil  = opd.opd_new_pupil * mask_pupil
    opd_segs   = opd.combine_opd_segs(opd.opd_new_segs) * mask_pupil
    opd_resid  = (opd.opd - (opd_pupil+opd_segs)) * mask_pupil

    # OPD for science target observation
    opd_sci = opd_pupil + opd_segs + opd_resid

    # Return the residuals for use in drifted
    return (opd_sci, opd_resid)

def opd_sci_gen_mp(opds_all, nproc=None):
    if nproc is None:
        nopd = len(opds_all)
        nproc = int(np.min([nopd,mp.cpu_count()*0.75]))

    pool = mp.Pool(nproc)
    output = pool.map(opd_sci_gen, opds_all)
    pool.close()
    pool.join()

    #opd_sci_list, opd_resid_list = zip(*output)
    return zip(*output)

def opd_ref_gen(args, verbose=False):
    """Generate a drifted OPD image"""
    opd, wfe_drift, pup_cf_std, seg_cf_std, opd_resid, case = args
    np.random.seed()

    # Pupil mask (incl. segment spaces and secondary struts)
    mask_pupil = opd.mask_pupil * opd.mask_opd

    # Case of Pupil Only
    if case==1:
        var = pup_cf_std**2
        std = np.sqrt(var/var.sum()) * (wfe_drift/1000)
        rand = np.random.randn(std.size) * std
        coeff_pupil_new = opd.coeff_pupil + rand #delta_coeff[:pup_var.size]
        ncf = pup_cf_std[pup_cf_std!=0].size

        opd_pupil_new = opd._opd_from_coeff(coeff_pupil_new, opd.basis_zernike, mask_pupil) * mask_pupil
        opd_segs_new  = opd.combine_opd_segs(opd.opd_new_segs) * mask_pupil

        # Total
        opd_sci = opd.opd * mask_pupil
        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

        # Take the difference and make sure the RMS WFE difference is correct
        # Add this to the overall pupil coefficients
        opd_diff = (opd_sci - opd_ref) * mask_pupil
        rms_diff = opd_diff[opd_diff!=0].std()
        delta_rms = wfe_drift/1000 - rms_diff
        ind = var>0
        coeff_pupil_new[ind] += 1.1 * delta_rms * (rand[ind]/np.abs(rand[ind])) / np.sqrt(var[ind].size)
        opd_pupil_new = opd._opd_from_coeff(coeff_pupil_new, opd.basis_zernike, mask_pupil) * mask_pupil

        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

    # Case of Segments Only
    elif case==2:
        opd_pupil_new = opd.opd_new_pupil # No drift to overall pupil

        # Segments
        # Random Gaussian noise distributed across each Zernike coeff
        rand_all = []
        coeff_segs_new = []
        var = seg_cf_std**2
        std = np.sqrt(var/var.sum()) * (wfe_drift/1000)
        for cf in opd.coeff_segs:
            rand = np.random.randn(std.size) * std
            rand_all.append(rand)
            coeff_segs_new.append(cf + rand)

        opd_segs_new_list = opd._get_opd_new_segs(coeff_segs_new)
        opd_segs_new = opd.combine_opd_segs(opd_segs_new_list) * mask_pupil

        # Total
        opd_sci = opd.opd * mask_pupil
        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

        # Take the difference and make sure the RMS WFE difference is correct
        opd_diff = (opd_sci - opd_ref) * mask_pupil
        # Add this to the overall segment coefficients
        rms_diff = opd_diff[opd_diff!=0].std()
        delta_rms = wfe_drift/1000 - rms_diff
        ind = var>0; nind = var[ind].size
        for i,cf in enumerate(coeff_segs_new):
            rand = rand_all[i]
            cf[ind] += delta_rms * (rand[ind]/np.abs(rand[ind])) / np.sqrt(nind)
        opd_segs_new_list = opd._get_opd_new_segs(coeff_segs_new)
        opd_segs_new = opd.combine_opd_segs(opd_segs_new_list) * mask_pupil

        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

    # Case of Pupil and Segments distributed evenly
    elif case==3:
        # Pupil
        var = pup_cf_std**2
        std = np.sqrt(var/var.sum()) * (wfe_drift/1000)
        rand_pup = np.random.randn(std.size) * std / np.sqrt(2.0)
        coeff_pupil_new = opd.coeff_pupil + rand_pup #delta_coeff[:pup_var.size]
        opd_pupil_new = opd._opd_from_coeff(coeff_pupil_new, opd.basis_zernike, mask_pupil) * mask_pupil

        # Segments
        # Random Gaussian noise distributed across each Zernike coeff
        coeff_segs_new = []
        var = seg_cf_std**2
        std = np.sqrt(var/var.sum()) * (wfe_drift/1000) / np.sqrt(2.0)
        for cf in opd.coeff_segs:
            rand = np.random.randn(std.size) * std
            coeff_segs_new.append(cf + rand)

        #coeff_segs_new = [cf + np.random.normal(0,wfe_drift/1000,cf.shape) for cf in opd.coeff_segs]
        opd_segs_new_list = opd._get_opd_new_segs(coeff_segs_new)
        opd_segs_new = opd.combine_opd_segs(opd_segs_new_list) * mask_pupil


        # Total
        opd_sci = opd.opd * mask_pupil
        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

        # Take the difference and make sure the RMS WFE difference is correct
        # Add this to the overall pupil coefficients
        opd_diff = (opd_sci - opd_ref) * mask_pupil
        rms_diff = opd_diff[opd_diff!=0].std()
        delta_rms = wfe_drift/1000 - rms_diff
        ind = (pup_cf_std**2)>0; nind = rand_pup[ind].size
        coeff_pupil_new[ind] += 1.1 * delta_rms * (rand_pup[ind]/np.abs(rand_pup[ind])) / np.sqrt(nind)
        #coeff_pupil_new += delta_rms
        #coeff_pupil_new[pup_cf_std!=0] -= delta_rms
        opd_pupil_new = opd._opd_from_coeff(coeff_pupil_new, opd.basis_zernike, mask_pupil) * mask_pupil

        opd_ref = opd_pupil_new + opd_segs_new + opd_resid

    # Jeremy's Method
    elif case==4:
        opd_sci = opd.opd * mask_pupil
        opd_ref = opd_drift_nogood(opd_sci, wfe_drift) * mask_pupil


    if verbose:
        opd_sci = opd.opd * mask_pupil
        opd_diff = (opd_sci - opd_ref) * mask_pupil
        print('Sci RMS: {:.3f}, Ref RMS: {:.3f}, RMS diff: {:.4f}' \
              .format(opd_sci[opd_sci!=0].std(), opd_ref[opd_ref!=0].std(), opd_diff[opd_diff!=0].std()))

    return opd_ref

# Function to drift a list of  OPDs 
def ODP_drift_all(wfe_drift, opds_all, pup_cf_std, seg_cf_std, opd_resid_list):
    """
    Drift a list of OPDs by some RMS WFE (multiprocess function)

    Args:
        wfe_drift - Singel value in nm
        opds_all - List of OPD objects (usually 10 from RevV OPDs)
        pup_cf_std - Zernike sigma for overall pupil
        seg_cf_std - Hexike sigma for segments
        opd_resid_list - List of residual images (from opd_sci_gen) for each OPD
    """

    case = 1 # Only drift full pupil; no segment drifts

    nopd = len(opds_all)
    nproc = int(np.min([nopd,mp.cpu_count()*0.75]))

    pool = mp.Pool(nproc)
    worker_arguments = [(opds_all[i], wfe_drift, pup_cf_std, seg_cf_std, 
                         opd_resid_list[i], case) for i in range(nopd)]
    out = pool.map(opd_ref_gen, worker_arguments)
    pool.close()
    pool.join()

    print('Finished: {:.0f} nm'.format(wfe_drift))

    return out

def get_psf(opd, header, filter='F410M', mask=None, pupil=None, 
    jitter=False, njit=10, jitter_sigma=0.005, r=None, theta=None, **kwargs):
    """
    Create a NIRCam PSF based on the input opd, header, and other info.

    The jitter model recomputes the PSF at random positional offsets with
    a standard deviation equal to jitter_sigma. It does this njit times
    and averages the resulting PSFs. This 
    """

    hdu = fits.PrimaryHDU(opd)
    hdu.header = header.copy()
    opd_hdulist = fits.HDUList([hdu])    

    nc = webbpsf.NIRCam()
    nc.pupilopd   = opd_hdulist 
    nc.filter     = filter
    nc.image_mask = mask
    nc.pupil_mask = pupil
    nc.options['parity'] = 'odd'

    # Reposition stellar spot to optimal position on bar if LWB or SWB
    # If no bar mask, then this function returns 0s
    r_nom, theta_nom = offset_bar(filter,mask)
    r     = r_nom     if r     is None else r
    theta = theta_nom if theta is None else theta
    nc.options['source_offset_r']     = r
    nc.options['source_offset_theta'] = theta	
    #nc.options['output_mode'] = 'both'

    nc.options['jitter'] = None
    if jitter and (jitter_sigma>0):
        jit_type = 'Full'
        offset_x, offset_y = nrc_utils.rtheta_to_xy(r, theta)
        for i in range(njit):
            # Random position offset
            jit_off = np.random.normal(0, jitter_sigma)
            jit_ang = np.random.uniform(0, 180)
        
            # Convert jitter offset to (x,y) position and add to source location 
            jit_x, jit_y = nrc_utils.rtheta_to_xy(jit_off, jit_ang)
            off_x_new = offset_x + jit_x
            off_y_new = offset_y + jit_y
            rtemp, ttemp = nrc_utils.xy_to_rtheta(off_x_new, off_y_new)
            
            # Save position offsets
            nc.options['source_offset_r']     = rtemp
            nc.options['source_offset_theta'] = ttemp
            hdulist = nc.calc_psf(fov_arcsec=10)				
            if i==0:
                data0 = np.zeros_like(hdulist[0].data)
                data1 = np.zeros_like(hdulist[1].data)
            # Sum up the PSFs
            data0 += hdulist[0].data; data1 += hdulist[1].data
        
        # Average jittered data
        hdulist[0].data = data0 / njit
        hdulist[1].data = data1 / njit
    else:
        hdulist = nc.calc_psf(fov_arcsec=10)
        jit_type = 'None'
        jitter_sigma = 0

#     if jitter is not None:
#         if jitter.lower() == 'gaussian':
#             nc.options['jitter'] = 'gaussian'
#             nc.options['jitter_sigma'] = jitter_sigma
#             hdulist = nc.calc_psf(fov_arcsec=10)
#         elif jitter.lower() == 'full':
#             nc.options['jitter'] = None
#             njit = 10
#             for i in range(njit):
#                 rtemp = r + np.random.normal(0, jitter_sigma)
#                 ttemp = theta + np.random.uniform(0,360)
#                 nc.options['source_offset_r']     = rtemp
#                 nc.options['source_offset_theta'] = ttemp
#                 hdulist = nc.calc_psf(fov_arcsec=10)				
#                 if i==0:
#                     data0 = np.zeros_like(hdulist[0].data)
#                     data1 = np.zeros_like(hdulist[1].data)
#                 # Sum up the PSFs
#                 data0 += hdulist[0].data; data1 += hdulist[1].data
#             # Average jittered data
#             hdulist[0].data = data0 / njit
#             hdulist[1].data = data1 / njit
#         else:
#             raise ValueError('Do not recognize jitter = '+jitter)
#     else: # Create nominal PSF, no jitter
#         hdulist = nc.calc_psf(fov_arcsec=10)

    xval, yval = nrc_utils.rtheta_to_xy(r, theta)
    xval = 0 if np.isclose(0, xval, atol=1e-4) else xval
    yval = 0 if np.isclose(0, yval, atol=1e-4) else yval
    for hdr in [hdulist[0].header, hdulist[1].header]:
        hdr.insert('extname', ('offset',     r, 'Source offset from center in arcsec'))
        hdr.insert('extname', ('theta',  theta, 'Degrees CCW from inst +Y in sci frame'))
        hdr.insert('extname', ('xoffset', xval, 'Source xoffset in sci frame (arcsec)'))
        hdr.insert('extname', ('yoffset', yval, 'Source yoffset in sci frame (arcsec)'))
        hdr.insert('extname', ('JITRTYPE', jit_type, 'Type of jitter applied'))
        hdr.insert('extname', ('JITRSIGM', jitter_sigma, 'Gaussian sigma for jitter [arcsec]'))

    return hdulist


def offset_bar(filt, mask):
    """
    Get the appropriate offset in the x-position to place a source on a bar mask.
    Each bar is 20" long with edges and centers corresponding to:
      SWB: [1.03, 2.10, 3.10] (um) => [-10, 0, +10] (asec)
      LWB: [2.30, 4.60, 6.90] (um) => [+10, 0, -10] (asec)
    """

    if (mask is not None) and ('WB' in mask):		
        # What is the effective wavelength of the filter?	
        #bp = pynrc.read_filter(filter)
        #w0 = bp.avgwave() / 1e4
        w0 = np.float(filt[1:-1])/100
    
        # Choose wavelength from dictionary
        wdict = {'F182M': 1.84, 'F187N': 1.88, 'F210M': 2.09, 'F212N': 2.12, 
                 'F250M': 2.50, 'F300M': 2.99, 'F335M': 3.35, 'F360M': 3.62, 
                 'F410M': 4.09, 'F430M': 4.28, 'F460M': 4.63, 'F480M': 4.79,
                 'F200W': 2.23, 'F277W': 3.14, 'F356W': 3.97, 'F444W': 4.99}
        w = wdict.get(filt, w0)

        # Get appropriate x-offset
        #xoff_asec = np.interp(w,wpos,xpos)
    
        if 'SWB' in mask:
            if filt[-1]=="W": xoff_asec = 6.83 * (w - 2.196)
            else:             xoff_asec = 7.14 * (w - 2.100)
        elif 'LWB' in mask:
            if filt[-1]=="W": xoff_asec = -3.16 * (w - 4.747)
            else:             xoff_asec = -3.26 * (w - 4.600)
        
        print(w,xoff_asec)
        
        yoff_asec = 0.0
    
        r, theta = nrc_utils.xy_to_rtheta(xoff_asec, yoff_asec)
    else:
        r, theta = (0.0, 0.0)
    
    return r, theta


def gen_psf_ref_all(opd_ref_list_all, opd_header, filt, mask, pupil, verbose=False, **kwargs):
    """
    Given a list of reference OPD, calculate a series of PSFs.
    Assume that opd_ref_list_all is a nested list of OPDs.
    For instance:
        [[10 OPDs with 1nm drift], [10 OPDs with 5nm drift], [10 OPDs with 10nm drift]]
    """
    psf_ref_all = []
    npsf = len(opd_ref_list_all)
    for i in range(npsf):
        opd_ref_list = opd_ref_list_all[i]
        psf_ref = [get_psf(opd, opd_header, filt, mask, pupil, **kwargs) for opd in opd_ref_list]
        psf_ref_all.append(psf_ref)
        if verbose: print('Finished {} of {}.'.format(i+1, npsf))

    return psf_ref_all

def get_contrast_old(psf0,psf1,psf2):
    """
    For science and reference PSFs, return the contrast curve.
    Assumes no noise other than residual speckle noise.
    psf0 is the planet PSF
    psf1 is the stellar PSF
    psf2 is the reference PSF
    """

    # PSF subtraction
    from copy import deepcopy
    psf_diff = deepcopy(psf1)
    psf_diff[0].data = (psf1[0].data - psf2[0].data)
    psf_diff[1].data = (psf1[1].data - psf2[1].data)

    # Radial noise profiles of PSF difference
    rr0, stds0 = webbpsf.radial_profile(psf_diff, ext=0, stddev=True)
    #rr1, stds1 = webbpsf.radial_profile(psf_diff, ext=1, stddev=True)

    ## Total planet signal at a radius of 0.5"
    #rr_psf0, mn_psf0, ee_psf0 = webbpsf.radial_profile(psf0, ext=0, EE=True)
    #rad_asec = 0.5
    #npix = np.pi * (rad_asec / psf0[0].header['PIXELSCL'])**2
    ## Get the encircled energy of planet at radius
    #planet_signal = np.interp(rad_asec, rr_psf0, ee_psf0)
    ## nsigma of planet signal relative to noise
    #contrast =  np.sqrt(stds0**2 * npix) / planet_signal

    contrast = stds0 / np.max(psf0[0].data)

    return rr0, contrast


def get_contrast(speckle_noise_image, planet_psf):
    """
    Return the contrast for curve for a speckle noise map

    Both inputs are HDULists
    """

    # Radial profile of the noise image
    ext = 0
    header = speckle_noise_image[ext].header

    pixelscale = header['PIXELSCL']
    xoff = header['XOFFSET'] / pixelscale
    yoff = header['YOFFSET'] / pixelscale

    xcen = header['NAXIS1'] / 2.0 + xoff
    ycen = header['NAXIS2'] / 2.0 + yoff

    rr0, stds0 = webbpsf.radial_profile(speckle_noise_image, ext=ext, center=(xcen,ycen))
    #rr1, stds1 = webbpsf.radial_profile(psf_diff, ext=1)

    contrast = stds0 / np.max(planet_psf[0].data)

    return rr0, contrast


def get_contrast_all_old(psf_planet, psf_star_all, psf_ref_all):
    """
    Return a list of contrast curves based on a list of PSFs
    """
    nopd = len(psf_star_all)
    return [get_contrast_old(psf_planet,psf_star_all[i],psf_ref_all[i]) for i in range(nopd)]

def residual_speckle_image(psf_star_all, psf_ref_all):
    """
    For a list of science and reference PSFs, create residual speckle images
    """
    nopd = len(psf_star_all)
    psf1 = psf_star_all[0] # Stellar PSF
    psf2 = psf_ref_all[0]  # Reference PSF

    # Do the difference for each extension (pixel-sampled and oversampled)
    ny0,nx0 = psf1[0].data.shape
    ny1,nx1 = psf1[1].data.shape

    diff_all0 = np.zeros([nopd,ny0,nx0])
    diff_all1 = np.zeros([nopd,ny1,nx1])

    for i in range(nopd):
        diff_all0[i,:,:] = psf_star_all[i][0].data - psf_ref_all[i][0].data
        diff_all1[i,:,:] = psf_star_all[i][1].data - psf_ref_all[i][1].data

    return diff_all0, diff_all1

def speckle_noise_image(psf_star_all, psf_ref_all):
    """
    For a list of science and reference PSFs, create a speckle noise image map
    """

    nopd = len(psf_star_all)
    psf1 = psf_star_all[0] # Stellar PSF
    psf2 = psf_ref_all[0]  # Reference PSF

    # Do the difference for each extension (pixel-sampled and oversampled)
    ny0,nx0 = psf1[0].data.shape
    ny1,nx1 = psf1[1].data.shape

    diff_all0 = np.zeros([nopd,ny0,nx0])
    diff_all1 = np.zeros([nopd,ny1,nx1])

    for i in range(nopd):
        diff_all0[i,:,:] = psf_star_all[i][0].data - psf_ref_all[i][0].data
        diff_all1[i,:,:] = psf_star_all[i][1].data - psf_ref_all[i][1].data

    # Create HDU to hold standard deviations
    from copy import deepcopy
    psf_diff = deepcopy(psf1)

    # Save the standard deviation of the speckle noise for each extension
    psf_diff[0].data = diff_all0.std(axis=0)
    psf_diff[1].data = diff_all1.std(axis=0)

    return psf_diff


def opd_drift_nogood(opd, drift, nterms=8, defocus_frac=0.8):
    """
    Add some WFE drift (in nm) to an OPD image.

    Parameters
    ------------
    opd    : OPD images (can be an array of images).
    header : Header file
    drift  : WFE drift in nm

    Returns
    --------
    Returns an HDUList, which can be passed to webbpsf
    """


    # Various header info for coordinate grid
    diam  = 6.55118110236
    pix_m = 0.00639763779528
    wfe_rm = 132.

    # Create x/y carteesian grid
    sh = opd.shape
    if len(sh) == 3: 
        nz,ny,nx = sh
        opd_sum = opd.sum(axis=0)
        mask0 = (opd_sum == 0)
        mask1 = (opd_sum != 0)
    else:
        nz = 1
        ny,nx = sh
        # Masks for those values equal (and not) to 0
        mask0 = (opd == 0)
        mask1 = (opd != 0)

    y,x = np.indices((ny,nx))
    center = tuple((a - 1) / 2.0 for a in [nx,ny])
    y = y - center[1]; x = x - center[0]
    y *= pix_m; x *= pix_m

    # Convert to polar coordinates
    rho = np.sqrt(x**2 + y**2) / (diam / 2)
    theta = np.arctan2(y,x)

    # Generate Zernike maps
    # Drop the piston
    zall = (poppy.zernike.zernike_basis(nterms, rho=rho, theta=theta))[1:,:,:]
    zall[:,mask0] = 0

    # Sum Zernikes and normalize to total
    # Exclude defocus term
    zmost = np.concatenate((zall[0:2,:,:], zall[3:,:,:]))
    ztot = zmost.sum(axis=0)
    ztot /= ztot.sum()

    # Normalize defocus
    zfoc = zall[2,:,:]
    zfoc /= zfoc.sum()

    # Fraction of total that goes into defocus
    zsum = (1.0-defocus_frac)*ztot + defocus_frac*zfoc

    # Set masked pixels to 0 and normalize to unmasked sigma
    zsum[mask0] = 0
    zsum /= zsum[mask1].std()

    # RMS factor measured versus ideal
    # Accounts for unit differences as well (meters vs nm)
    # header['WFE_RMS'] is in nm, as is drift
    if len(sh) == 3:
        rms_opd = np.array([(opd[i,:,:])[mask1].std() for i in range(nz)])
        rms_fact = rms_opd / wfe_rm
        drift_act = rms_fact * drift
        zadd = zsum * drift_act.reshape([nz,1,1]) # Array broadcasting
    else:
        drift_act = drift * opd[mask1].std() / wfe_rm
        zadd = zsum * drift_act

    return opd + zadd

def _opd_drift_nogood(opd, header, drift, nterms=8, defocus_frac=0.8):
    """
    Add some WFE drift (in nm) to an OPD image.

    Parameters
    ------------
    opd    : OPD images (can be an array of images).
    header : Header file
    drift  : WFE drift in nm

    Returns
    --------
    Returns an HDUList, which can be passed to webbpsf
    """


    # Various header info for coordinate grid
    diam = header['PUPLDIAM']
    pix_m = header['PUPLSCAL']

    # Create x/y carteesian grid
    sh = opd.shape
    if len(sh) == 3: 
        nz,ny,nx = sh
        opd_sum = opd.sum(axis=0)
        mask0 = (opd_sum == 0)
        mask1 = (opd_sum != 0)
    else:
        nz = 1
        ny,nx = sh
        # Masks for those values equal (and not) to 0
        mask0 = (opd == 0)
        mask1 = (opd != 0)

    y,x = np.indices((ny,nx))
    center = tuple((a - 1) / 2.0 for a in [nx,ny])
    y = y - center[1]; x = x - center[0]
    y *= pix_m; x *= pix_m

    # Convert to polar coordinates
    rho = np.sqrt(x**2 + y**2) / diam
    theta = np.arctan2(y,x)

    # Generate Zernike maps
    zall = (poppy.zernike.zernike_basis(nterms, rho=rho, theta=theta))[1:,:,:]

    # Sum Zernikes and noramlize to total
    ztot = zall.sum(axis=0)
    ztot /= ztot.sum()

    # Fraction of total that goes into defocus
    ztot = (1.0-defocus_frac)*ztot + defocus_frac*(zall[2,:,:]/zall[2,:,:].sum())

    # Set masked pixels to 0 and normalize to unmasked sigma
    ztot[mask0] = 0
    ztot /= ztot[mask1].std()

    # RMS factor measured versus ideal
    # Accounts for unit differences as well (meters vs nm)
    # header['WFE_RMS'] is in nm, as is drift
    if len(sh) == 3:
        rms_opd = np.array([(opd[i,:,:])[mask1].std() for i in range(nz)])
        rms_fact = rms_opd / header['WFE_RMS']
        drift_act = rms_fact * drift
        zadd = ztot * drift_act.reshape([nz,1,1]) # Array broadcasting
    else:
        drift_act = drift * opd[mask1].std() / header['WFE_RMS']
        zadd = ztot * drift_act

    # Add Zernike drifts to original OPD
    hdu = fits.PrimaryHDU(opd + zadd)
    hdu.header = header.copy()
    hdu.header['history'] = 'Added {:.1f} nm of WFE drift.'.format(drift)
    hdulist = fits.HDUList([hdu])
    
    return hdulist

