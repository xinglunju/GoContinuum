#!/bin/python
import os, argparse

import numpy as np
import matplotlib.pyplot as plt
from myutils.logger import get_logger
from scipy.ndimage.morphology import binary_dilation
from scipy.stats import linregress
from astropy.stats import sigma_clip
from myutils.argparse_actions import LoadFITS, LoadTXTArray
from extract_spectra import find_peak

# Start settings
logger = get_logger(__name__, filename='continuum_iterative.log')

def group_chans(inds):
    """ Group contiguous channels.
    Notes:
        Taken from:
        https://stackoverflow.com/questions/7352684/how-to-find-the-groups-of-consecutive-elements-from-an-array-in-numpy
    """
    return np.split(inds, np.where(np.diff(inds) != 1)[0]+1)

def filter_min_width(mask, min_width=2):
    if np.all(mask):
        return mask
    ind = np.arange(mask.size)
    groups = group_chans(ind[mask])
    for g in groups:
        if len(g)<=min_width:
            mask[g[0]:g[-1]+1] = False
    return mask

def chans_to_casa(chans, sep=';'):
    """Create a string with in CASA format with the channel ranges.
    """
    chanstr = ''
    for ch in chans:
        if len(ch)==1:
            chanstr += '%i%s' % (ch, sep) 
        else:
            chanstr += '%i~%i%s' % (ch[0], ch[-1], sep)
    return chanstr.strip(sep)

def linreg_stat(x, axis=None):
    # Data arrays
    try:
        xnew = np.arange(x.data.size, dtype=float)[~x.mask]
        ynew = x.data[~x.mask]

        # At the center of the spw
        xnew = xnew - x.data.size/2

    except AttributeError:
        xnew = np.arange(x.size)
        ynew = x

        # At the center of the spw
        xnew = xnew - x.size/2

    # Regression
    slope, intercept, r_value, p_value, std_err = linregress(xnew, ynew)

    return intercept

def find_continuum(spec, sigma_lower=3.0, sigma_upper=1.3, niter=None,
        cenfunc=np.ma.median, edges=10, erode=0, min_width=2, min_space=0, 
        table=None, log=True):
    # Spec to masked array
    spec = np.ma.masked_invalid(spec)

    # Filter edges
    assert edges<spec.size
    if edges>0:
        if log:
            logger.info('Masking values at extremes channels')
        spec.mask[:edges] = True
        spec.mask[-edges:] = True

    # Filter data
    specfil = sigma_clip(spec, sigma_lower=sigma_lower, 
            sigma_upper=sigma_upper, iters=niter, cenfunc=cenfunc)
    if log:
        nfil = np.ma.count_masked(specfil)
        ntot = specfil.data.size
        logger.info('Initial number of masked channels = %i/%i', nfil, ntot)

    # Erode lines
    assert erode<specfil.data.size/2
    if erode>0:
        if log:
            logger.info('Eroding the lines %i times', erode)
        specfil.mask = binary_dilation(specfil.mask, iterations=erode)
        if log:
            nfil = np.ma.count_masked(specfil)
            logger.info('Number of masked channels after eroding = %i/%i', nfil, ntot)

    # Filter small bands
    if min_width>0:
        if log:
            logger.info('Filtering out small masked bands')
            logger.info('Minimum masked band width: %i', min_width)
        specfil.mask = filter_min_width(specfil.mask, min_width)
        if log:
            nfil = np.ma.count_masked(specfil)
            logger.info('Number of masked channels after unmasking small bands = %i/%i', 
                    nfil, ntot)
        
    # Filter consecutive
    if min_space is not None and min_space>1:
        if log:
            logger.info('Filtering out small bands')
        ind = np.arange(specfil.mask.size)
        groups = np.split(ind[specfil.mask], 
                np.where(np.diff(ind[specfil.mask]) > min_space+1)[0]+1)
        for g in groups:
            if len(g)>1:
                specfil.mask[g[0]:g[-1]] = True
        if log:
            nfil = np.ma.count_masked(specfil)
            logger.info('Number of masked channels after masking consecutive = %i/%i', 
                    nfil, ntot)

    # Continuum
    cont = np.ma.mean(specfil)
    cstd = np.ma.std(specfil)
    if log:
        nfil = np.ma.count_masked(specfil)
        logger.info('Final number of masked channels = %i/%i', nfil, ntot)
        logger.info('Continuum level = %f+/-%f', cont, cstd)
    if table is not None:
        table.write('%10f\t%10f\t%10i\n' % (cont, cstd, nfil))

    return specfil, cont, cstd

def preprocess(args):
    # Initialize table
    if args.table is not None:
        fmt = '%s\t' * len(args.tableinfo)
        args.table.write(fmt % tuple(args.tableinfo))

    try:
        # If cube is loaded
        logger.info('Image shape: %r', args.cube.data.shape)
        assert args.cube.data.ndim == 4
        assert args.cube.data.shape[0]==1
        # Find peak
        if args.peak is not None:
            # User value
            logger.info('Using input peak position')
            xmax, ymax = args.peak
        else:
            ## Sum over spectral axis
            #if args.rms is not None:
            #    logger.info('Summing along spectral axis data over %f',
            #            args.rms[0])
            #    masked = np.ma.masked_less(args.cube.data[0,:,:,:], args.rms[0])
            #    imgsum = np.sum(masked, axis=0)
            #else:
            #    logger.info('Summing along spectral axis')
            #    imgsum = np.sum(args.cube.data[0,:,:,:], axis=0)
            #ymax, xmax = np.unravel_index(np.nanargmax(imgsum), imgsum.shape)
            #logger.info('Peak pixel at: %i, %i', xmax, ymax)
            xmax, ymax = find_peak(cube=args.cube, rms=args.rms)

        # Write table
        if args.table is not None:
            args.table.write('%5i\t%5i\t' % (xmax, ymax))

        # Get spectrum at peak
        if args.beam_avg:
            # Beam size
            logger.info('Averaging over beam')
            pixsize = np.sqrt(np.abs(args.cube.header['CDELT1'] * \
                    args.cube.header['CDELT2']))
            if args.beam_fwhm:
                beam_fwhm = args.beam_fwhm[0]/3600.
            else:
                beam_fwhm = np.sqrt(args.cube.header['BMIN'] * \
                        args.cube.header['BMAJ'])
            if args.beam_size:
                beam_sigma = args.beam_size[0]/3600.
            else:
                beam_sigma = beam_fwhm / (2.*(2.*np.log(2))**0.5)
            beam_sigma = beam_sigma / pixsize
            logger.info('Beam size (sigma) = %f pix', beam_sigma)

            # Filter data
            Y, X = np.indices(args.cube.data.shape[2:])
            dist = np.sqrt((X-xmax)**2. + (Y-ymax)**2.)
            mask = dist<=beam_sigma
            masked = np.ma.array(args.cube.data[0,:,:,:], 
                    mask=np.tile(~mask,(args.cube.data[0,:,:,:].shape[0],1)))
            args.spectrum = np.sum(masked, axis=(1,2))/np.sum(mask)
        else:
            logger.info('Using peak spectra')
            args.spectrum = args.cube.data[0,:,ymax,xmax]
        logger.info('Number of channels: %i', len(args.spectrum))
        if args.specname:
            with open(os.path.expanduser(args.specname), 'w') as out:
                out.write('\n'.join(['%f %f' % fnu for fnu in \
                        enumerate(args.spectrum)]))

        ## Off source reference spectrum
        #if args.ref_pix is not None:
        #    args.ref_spec = np.ma.array(args.cube.data[0,:,
        #        args.ref_pix[1],args.ref_pix[0]], mask=False)
        #    logger.info('Reference pixel mean: %f', np.mean(args.ref_spec))

    except AttributeError:
        # If spectrum is loaded from file
        logger.info('Spectrum shape: %r', args.spec.shape)
        if len(args.spec.shape)>1:
            logger.info('Selecting second column')
            args.spectrum = args.spec[:,1]
        else:
            args.spectrum = args.spec

        # Write table
        if args.table is not None:
            args.table.write('%5s\t%5s\t' % ('--', '--'))

def func_sigmaclip(args):
    logger.info('Using: sigma_clip')
    logger.info('Sigma clip iters = %r', args.niter)

    # Preporcess sigmaclip options
    if args.censtat == 'linregress':
        args.censtat = linreg_stat
    elif args.censtat == 'mean':
        args.censtat = np.ma.mean
    else:
        args.censtat = np.ma.median
    if len(args.sigma)==1:
        #sigma = args.sigma[0]
        sigma_lower = args.sigma[0]
        sigma_upper = args.sigma[0]
    elif len(args.sigma)==2:
        #sigma = 1.8
        sigma_lower = args.sigma[0]
        sigma_upper = args.sigma[1]
    else:
        raise ValueError

    ## Filter data
    #filtered = sigma_clip(args.spectrum, sigma=sigma, sigma_lower=sigma_lower, 
    #        sigma_upper=sigma_upper, iters=args.niter,cenfunc=args.censtat)
    #nfil = np.sum(filtered.mask)
    #ntot = filtered.data.size
    #logger.info('Initial number of masked channels = %i/%i', nfil, ntot)

    ## Erode lines
    #assert args.erode<filtered.data.size/2
    #if args.erode>0:
    #    logger.info('Eroding the lines %i times', args.erode)
    #    filtered.mask = binary_dilation(filtered.mask, iterations=args.erode)
    #    nfil = np.sum(filtered.mask)
    #    logger.info('Number of masked channels after eroding = %i/%i', nfil, ntot)

    ## Filter small bands
    #if args.min_width:
    #    logger.info('Filtering out small masked bands')
    #    logger.info('Minimum masked band width: %i', args.min_width)
    #    filtered.mask = filter_min_width(filtered.mask, args.min_width)
    #    nfil = np.sum(filtered.mask)
    #    logger.info('Number of masked channels after unmasking small bands = %i/%i', 
    #            nfil, ntot)
    #    
    ## Filter consecutive
    #if args.min_space:
    #    logger.info('Filtering out small bands')
    #    ind = np.arange(filtered.mask.size)
    #    groups = np.split(ind[filtered.mask], 
    #            np.where(np.diff(ind[filtered.mask]) > min_space+1)[0]+1)
    #    for g in groups:
    #        if len(g)>1:
    #            filtered.mask[g[0]:g[-1]] = True
    #    nfil = np.sum(filtered.mask)
    #    logger.info('Number of masked channels after masking consecutive = %i/%i', 
    #            nfil, ntot)

    ## Filter extremes
    #assert args.extremes<filtered.data.size
    #if args.extremes>0:
    #    logger.info('Masking values at extremes channels')
    #    filtered.mask[:args.extremes+1] = True
    #    filtered.mask[-args.extremes:] = True

    ## Continuum
    #cont = np.mean(filtered)
    #cstd = np.std(filtered)
    #nfil = np.sum(filtered.mask)
    #logger.info('Final number of masked channels = %i/%i', nfil, ntot)
    #logger.info('Continuum level = %f+/-%f', cont, cstd)
    #if args.table is not None:
    #    args.table.write('%10f\t%10f\t%10i\n' % (cont, cstd, nfil))
    filtered, cont, cstd = find_continuum(args.spectrum,
            sigma_lower=sigma_lower, sigma_upper=sigma_upper, niter=args.niter,
            cenfunc=args.censtat, edges=args.extremes, erode=args.erode,
            min_width=args.min_width, min_space=args.min_space,
            table=args.table, log=True)
    nfil = np.ma.count_masked(filtered)
    ntot = filtered.data.size

    # Get sigma_clip steps
    scpoints, scmedians, scmeans, scstds = get_sigma_clip_steps(args.spectrum, 
            sigma_lower, sigma_upper, cenfunc=args.censtat)

    # Contiguous channels
    ind = np.arange(len(filtered.data))
    chans = group_chans(ind[filtered.mask])

    # Plot
    if args.plotname:
        # Plot difference in steps
        scpoint_fractions = 100.*scpoints/ntot
        #plt.loglog(np.abs(scpoints[1:]-scpoints[:-1]), np.abs(scstds[1:]-scstds[:-1]), 'ro')
        #plt.xlabel('Channel difference')
        #plt.ylabel('Std difference')
        #plt.savefig(args.plotname.replace('.png', '.compare_std.png'))
        #plt.close()

        #fig, ax1, ax2, ax1b = get_plot(ylabel1='Mean / Continuum', 
        #    ylabel1b="%% of channels (continuum = %i)" % (ntot-nfil))
        fig, ax1, ax2, ax1b = get_plot(ylabela='Mean / Continuum', 
            xlabel="%% of channels (continuum = %i)" % (ntot-nfil),
            ylabelb='Standard deviation')

        # Iterations
        #ax1.plot(1, cont/cont, 'bo')
        #ax1b.plot(1, 100.*(ntot-nfil)/ntot, 'ro')
        ax1.plot(100.*(ntot-nfil)/ntot, cont/cont, 'bo')
        ax1b.plot(100.*(ntot-nfil)/ntot, np.std(filtered), 'ro')
        #ax1.plot(scpoint_fractions, scmeans/cont, 'b+', markersize=40)
        #ax1b.plot(scpoint_fractions, scstds, 'r+', markersize=40)
        ax1.plot(scpoint_fractions, scmeans/cont, 'b+', markersize=20)
        ax1b.plot(scpoint_fractions, scstds, 'r+', markersize=20)
        for xl,xu,yl,yu in zip(scpoint_fractions[:-1], scpoint_fractions[1:], 
                scmeans[:-1], scmeans[1:]):
            percent = 100.*np.abs(yl-yu)/np.max([yl,yu])
            ax1.annotate('%.1f' % percent, (0.5*(xl+xu),0.5*(yl+yu)/cont),
                    xytext=(0.5*(xl+xu),0.5*(yl+yu)/cont), xycoords='data',
                    horizontalalignment='center', color='k')

        # Spectrum
        ax2.plot(filtered.data, 'k-')
        ax2.set_xlim(0, len(filtered.data))
        #ax2.set_xlim(2750, 3000)
        ax2.axhline(cont, color='b', linestyle='-')
        #ax2.axhline(0.15887, color='g', linestyle='-')
        plot_mask(ax2, chans)
    
    # Iteration condition
    #inc = 1. + args.increment/100.
    #i = 1.
    #def condition(inc):
    #    c1 = cont+inc*args.sigma*cstd <= np.max(filtered.data)
    #    c2 = cont-inc*args.sigma*cstd >= np.min(filtered.data)
    #    return c1 or c2
    #closest = 100
    #close_mask = None
    #while condition(inc):
    #    # Filter points
    #    mask = (filtered.data <= cont+inc*args.sigma*cstd) & \
    #            (filtered.data > cont-inc*args.sigma*cstd)
    #    newmean = np.mean(filtered.data[mask])
    #    newstd = np.std(filtered.data[mask])

    #    # Log
    #    #print('='*80)
    #    #logger.info('Iteration: %i', i+1)
    #    #logger.info('Upper limit = %f', cont+inc*cstd)
    #    #logger.info('Lower limit = %f', cont-inc*cstd)
    #    #logger.info('Mean value = %f', newmean)
    #    #logger.info('Std value = %f', newstd)

    #    # Expand mask
    #    aux = np.abs(np.sum(~mask)*100./nfil)
    #    if np.abs(aux - args.limit) < closest:
    #        closest = np.abs(aux - args.limit)
    #        close_mask = mask

    #    if args.plotname:
    #        #ax1.plot(i+1, newmean/cont, 'bo')
    #        #ax1b.plot(i+1, (ntot-np.sum(~mask))*100./ntot, 'ro')
    #        ax1.plot((ntot-np.sum(~mask))*100./ntot, newmean/cont, 'bo')
    #        ax1b.plot((ntot-np.sum(~mask))*100./ntot, newstd, 'ro')

    #    # Update increment
    #    i += 1
    #    inc = 1. + args.increment*i/100.

    ## Log
    #print('-'*80)
    #logger.info('Closest percent difference = %f', closest)
    #logger.info('Masked channels difference = %i', nfil-np.sum(~close_mask))

    if args.plotname:
        #chans = group_chans(ind[~close_mask])
        #plot_mask(ax2, chans, color='g')
        fig.savefig(args.plotname, bbox_inches='tight')

    return filtered.mask

def get_sigma_clip_steps(spec, sigma_lower, sigma_upper, cenfunc='median'):
    newcont = np.inf
    means = []
    medians = []
    stds = []
    npoints = []
    i = 1
    while True:
        filtered = sigma_clip(spec, sigma_lower=sigma_lower,
                sigma_upper=sigma_upper, iters=i, cenfunc=cenfunc)
        npoint = np.sum(~filtered.mask)
        if len(npoints)==0 or npoints[-1]!=npoint:
            npoints += [npoint]
            means += [np.mean(filtered)]
            medians += [np.median(spec[~filtered.mask])]
            stds += [np.std(filtered)]
        else:
            break
        i += 1
    return np.array(npoints), np.array(medians), np.array(means), \
            np.array(stds)

def get_plot(xlabel='Iteration number', ylabela='Average intensity', 
        ylabelb='Masked channels'):
    plt.close()
    width = 17.2
    height = 3.5
    fig = plt.figure(figsize=(width,height))
    ax1 = fig.add_axes([0.8/width,0.4/height,5/width,3/height])
    ax2 = fig.add_axes([7.0/width,0.4/height,10/width,3/height])

    # Plot cumulative sum
    ax1.set_xlabel(xlabel)
    ax1.set_ylabel(ylabela)
    ax1.tick_params('y', colors='b')

    # Plot spectrum
    ax2.set_xlabel('Channel number')
    ax2.set_ylabel('Intensity')

    ax1b = ax1.twinx()
    ax1b.set_ylabel(ylabelb)
    ax1b.tick_params('y', colors='r')

    return fig, ax1, ax2, ax1b

def plot_mask(ax, chans, color='r'):
    for g in chans:
        if len(g)==0:
            continue
        ax.axvspan(g[0], g[-1], fc=color, alpha=0.5, ls='-')

def postprocess(mask_flagged, args):
    # Group channels
    assert len(args.spectrum)==len(mask_flagged)
    ind = np.arange(len(args.spectrum)) 
    flagged = group_chans(ind[mask_flagged])

    # Covert to CASA format
    flagged = chans_to_casa(flagged)

    if args.chanfile:
        logger.info('Writing: %s', os.path.basename(args.chanfile))
        with open(os.path.expanduser(args.chanfile), 'w') as out:
            out.write(flagged)
    logger.info('Channels flagged in CASA notation: %s', flagged)

def main():
    # Command line options
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    #parser.add_argument('--minsize', default=100,
    #        help='Minimum  number of continuum channels (default=10)')
    parser.add_argument('--erode', default=0, type=int,
            help='Number of channels to erode on each side of a line')
    parser.add_argument('--extremes', default=10, type=int,
            help='Number of channels to map at the begining and end of spectrum')
    parser.add_argument('--min_space', default=None, type=int,
            help='Minimum space between masked bands')
    parser.add_argument('--min_width', default=2, type=int,
            help='Minimum number of channels per masked band')
    parser.add_argument('--niter', default=None, type=int,
            help='Number of iterations')
    parser.add_argument('--plotname', default=None,
            help='Plot file name')
    parser.add_argument('--beam_avg', action='store_true',
            help='Calculate a beam averaged spectrum')
    parser.add_argument('--peak', nargs=2, type=int, default=None,
            help='Peak position in x,y pixels')
    parser.add_argument('--rms', nargs=1, type=float, default=None,
            help='Image rms')
    parser.add_argument('--specname', default=None,
            help='Spectrum file name')
    parser.add_argument('--chanfile', default=None,
            help='File to save the channels masked')
    parser.add_argument('--table', default=None, type=argparse.FileType('a'),
            help='Table file to save results')
    parser.add_argument('--tableinfo', default=[''], nargs='*', type=str,
            help='Data for the first columns of the table')
    parser.set_defaults(spectrum=None, loader=preprocess, post=postprocess,
            ref_pix=None)
    # Groups
    group1 = parser.add_mutually_exclusive_group(required=True)
    group1.add_argument('--cube', action=LoadFITS, 
            help='Image file name')
    group1.add_argument('--spec', action=LoadTXTArray,
            help='Spectrum file name')
    group2 = parser.add_mutually_exclusive_group(required=False)
    group2.add_argument('--beam_size', nargs=1, type=float,
            help='Beam size (sigma) in arcsec')
    group2.add_argument('--beam_fwhm', nargs=1, type=float,
            help='Beam FWHM in arcsec')
    # Subparsers
    psigmaclip = subparsers.add_parser('sigmaclip', 
            help="Use sigma clip")
    psigmaclip.add_argument('--sigma', nargs='*', type=float, default=1.8,
            help="Sigma level")
    psigmaclip.add_argument('--increment', type=float, default=10.,
            help="Percent of increment of std value")
    psigmaclip.add_argument('--limit', type=float, default=90.,
            help="Percent of the total number of masked points to include")
    psigmaclip.add_argument('--ref_pix', type=int, default=None, nargs=2,
            help="Off source pixel position")
    psigmaclip.add_argument('--censtat', type=str, default='median',
            choices=['median', 'mean', 'linregress'],
            help="Statistic for sigma_clip cenfunc")
    psigmaclip.set_defaults(func=func_sigmaclip, ref_spec=None)
    args = parser.parse_args()
    args.loader(args)
    mask = args.func(args)
    args.post(mask, args)

if __name__=='__main__':
    main()
