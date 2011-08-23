#!/usr/bin/python
# encoding: utf-8
"""
appleupdates.py

Utilities for dealing with Apple Software Update.

"""
# Copyright 2009-2011 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import gzip
import hashlib
import os
import stat
import subprocess
import time
import urllib2
import urlparse

from Foundation import NSDate
from Foundation import CFPreferencesCopyAppValue
#from Foundation import CFPreferencesSetValue
from Foundation import CFPreferencesAppSynchronize
#from Foundation import kCFPreferencesAnyUser
from Foundation import kCFPreferencesCurrentUser
from Foundation import kCFPreferencesCurrentHost

import FoundationPlist
import launchd
import munkicommon
import munkistatus
import updatecheck


# Apple Software Update Catalog URLs.
DEFAULT_CATALOG_URLS = {
    '10.5': 'http://swscan.apple.com/content/catalogs/others/index-leopard.merged-1.sucatalog',
    '10.6': 'http://swscan.apple.com/content/catalogs/others/index-leopard-snowleopard.merged-1.sucatalog',
    '10.7': 'http://swscan.apple.com/content/catalogs/others/index-lion-snowleopard-leopard.merged-1.sucatalog.gz',
}

# Filename for results of softwareupdate -l -f <pathname>.
# This lists the currently applicable Apple updates in a
# very useful format.
APPLICABLE_UPDATES = 'ApplicableUpdates.plist'

# Path to the directory where local catalogs are stored, relative to
# munkicommon.pref('ManagedInstallDir').
LOCAL_CATALOG_DIR_REL_PATH = 'content/catalogs/'

# The pristine, untouched, but potentially gzipped catalog.
APPLE_DOWNLOAD_CATALOG_NAME = 'apple.sucatalog'

# The pristine, untouched, and extracted catalog.
APPLE_EXTRACTED_CATALOG_NAME = 'apple_index.sucatalog'
APPLE_EXTRACTED_CATALOG_REL_PATH = os.path.join(
    LOCAL_CATALOG_DIR_REL_PATH, APPLE_EXTRACTED_CATALOG_NAME)

# The catalog containing only updates in APPLICABLE_UPDATES.
# This is used to replicate a subset of the software update
# server data to our local cache.
FILTERED_CATALOG_NAME = 'filtered_index.sucatalog'
FILTERED_CATALOG_REL_PATH = os.path.join(
    LOCAL_CATALOG_DIR_REL_PATH, FILTERED_CATALOG_NAME)

# The catalog containing only updates to be downloaded and installed.
# We use this one when downloading Apple updates.
# In this case package URLs are still pointing to the
# software update server so we can download them, but the rest of the
# URLs point to our local cache.
LOCAL_DOWNLOAD_CATALOG_NAME = 'local_download.sucatalog'
LOCAL_DOWNLOAD_CATALOG_REL_PATH = os.path.join(
    LOCAL_CATALOG_DIR_REL_PATH, LOCAL_DOWNLOAD_CATALOG_NAME)

# Catalog with all URLs (including package URLs) pointed to local cache.
# We use this one during install phase.
# This causes softwareupdate -d -a to fail cleanly if we don't
# have the required packages already downloaded.
LOCAL_CATALOG_NAME = 'local_install.sucatalog'
LOCAL_CATALOG_REL_PATH = os.path.join(
    LOCAL_CATALOG_DIR_REL_PATH, LOCAL_CATALOG_NAME)



class Error(Exception):
    '''Class for domain specific exceptions.'''


class ReplicationError(Error):
    '''A custom error when replication fails'''


def swupdCacheDir(temp=True):
    '''Returns the local cache dir for our Software Update mini-cache.

    The temp cache directory is cleared upon install completion, while the
    non-temp is kept.'''
    ManagedInstallDir = munkicommon.pref('ManagedInstallDir')
    if temp:
        return os.path.join(ManagedInstallDir, 'swupd', 'mirror')
    else:
        return os.path.join(ManagedInstallDir, 'swupd')


def rewriteOneURL(full_url):
    '''Rewrites a single URL to point to our local replica.'''
    our_base_url = 'file://localhost' + urllib2.quote(swupdCacheDir())
    # only rewrite the URL if needed
    if not full_url.startswith(our_base_url):
        unused_scheme, unused_netloc, path, unused_query, unused_fragment = (
            urlparse.urlsplit(full_url))
        return our_base_url + path
    else:
        return full_url


def rewriteURLsForProduct(product, rewrite_pkg_urls=False):
    '''Rewrites the (Server)MetadataURLs to point to our local cache.'''
    if 'ServerMetadataURL' in product:
        product['ServerMetadataURL'] = rewriteOneURL(
            product['ServerMetadataURL'])
    for package in product.get('Packages', []):
        if rewrite_pkg_urls and 'URL' in package:
            package['URL'] = rewriteOneURL(package['URL'])
        if 'MetadataURL' in package:
            package['MetadataURL'] = rewriteOneURL(package['MetadataURL'])
    distributions = product['Distributions']
    for dist_lang in distributions.keys():
        distributions[dist_lang] = rewriteOneURL(distributions[dist_lang])


def rewriteURLs(catalog, rewrite_pkg_urls=False):
    '''Rewrites some URLs in the given catalog to point to our local replica.'''
    if 'Products' in catalog:
        for product_key in catalog['Products'].keys():
            product = catalog['Products'][product_key]
            rewriteURLsForProduct(product, rewrite_pkg_urls=rewrite_pkg_urls)


def replicateURLtoFilesystem(full_url, copy_only_if_missing=False):
    '''Downloads a URL and stores it in the same relative path on our
    filesystem. Returns a path to the replicated file.'''

    root_dir = swupdCacheDir()

    (unused_scheme, unused_netloc,
     path, unused_query, unused_fragment) = urlparse.urlsplit(full_url)
    relative_url = path.lstrip('/')
    relative_url = os.path.normpath(relative_url)
    local_file_path = os.path.join(root_dir, relative_url)
    local_dir_path = os.path.dirname(local_file_path)
    if copy_only_if_missing and os.path.exists(local_file_path):
        return local_file_path
    if not os.path.exists(local_dir_path):
        try:
            os.makedirs(local_dir_path)
        except OSError, oserr:
            raise ReplicationError(oserr)
    try:
        unused_status = updatecheck.getResourceIfChangedAtomically(
            full_url, local_file_path, resume=True)
    except updatecheck.MunkiDownloadError, err:
        raise ReplicationError(err)
    return local_file_path


def cacheSwupdMetadata():
    '''Copies ServerMetadata (.smd), Metadata (.pkm), and Distribution (.dist)
    files for the available updates to the local machine and writes a new
    sucatalog that refers to the local copies of these files.'''
    filtered_catalogpath = os.path.join(
        swupdCacheDir(), FILTERED_CATALOG_REL_PATH)
    catalog = FoundationPlist.readPlist(filtered_catalogpath)
    if not 'Products' in catalog:
        return

    for product_key in catalog['Products'].keys():
        munkicommon.display_status(
            'Caching metadata for product ID %s', product_key)
        product = catalog['Products'][product_key]
        if 'ServerMetadataURL' in product:
            replicateURLtoFilesystem(
                product['ServerMetadataURL'], copy_only_if_missing=True)

        for package in product.get('Packages', []):
            if 'MetadataURL' in package:
                munkicommon.display_status(
                    'Caching package metadata for product ID %s', product_key)
                replicateURLtoFilesystem(
                    package['MetadataURL'], copy_only_if_missing=True)

        distributions = product['Distributions']
        for dist_lang in distributions.keys():
            munkicommon.display_status(
                'Caching %s distribution for product ID %s',
                dist_lang, product_key)
            dist_url = distributions[dist_lang]
            replicateURLtoFilesystem(dist_url, copy_only_if_missing=True)

    # rewrite URLs to point to local resources
    rewriteURLs(catalog, rewrite_pkg_urls=False)
    # write out the rewritten catalog
    localcatalogpath = os.path.join(swupdCacheDir(), LOCAL_CATALOG_DIR_REL_PATH)
    if not os.path.exists(localcatalogpath):
        try:
            os.makedirs(localcatalogpath)
        except OSError, oserr:
            raise ReplicationError(oserr)
    localcatalogpathname = os.path.join(
        localcatalogpath, LOCAL_DOWNLOAD_CATALOG_NAME)
    FoundationPlist.writePlist(catalog, localcatalogpathname)

    rewriteURLs(catalog, rewrite_pkg_urls=True)
    localcatalogpathname = os.path.join(localcatalogpath, LOCAL_CATALOG_NAME)
    FoundationPlist.writePlist(catalog, localcatalogpathname)


def writeFilteredUpdateCatalog(updatelist):
    '''Write out a sucatalog containing only the updates listed in updatelist.

    updatelist is a list of ProductIDs.'''
    # our locally-cached catalog
    catalogpath = os.path.join(
        swupdCacheDir(), APPLE_EXTRACTED_CATALOG_REL_PATH)
    catalog = FoundationPlist.readPlist(catalogpath)
    if 'Products' in catalog:
        filtered_products = {}
        for key in updatelist:
            filtered_products[key] = catalog['Products'][key]
        catalog['Products'] = filtered_products
    filtered_catalogpath = os.path.join(
        swupdCacheDir(), FILTERED_CATALOG_REL_PATH)
    FoundationPlist.writePlist(catalog, filtered_catalogpath)


def run_softwareupdate(options_list, stop_allowed=False,
                       mode=None, results=None):
    '''Runs /usr/sbin/softwareupdate with options.

    Provides user feedback via command line or MunkiStatus'''
    if results == None:
        # we're not interested in the results,
        # but need to create a temporary dict anyway
        results = {}

    # we need to wrap our call to /usr/sbin/softwareupdate with a utility
    # that makes softwareupdate think it is connected to a tty-like
    # device so its output is unbuffered so we can get progress info
    #
    # Try to find our ptyexec tool
    # first look in the parent directory of this file's directory
    # (../)
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ptyexec_path = os.path.join(parent_dir, 'ptyexec')
    if not os.path.exists(ptyexec_path):
        # try absolute path in munki's normal install dir
        ptyexec_path = '/usr/local/munki/ptyexec'
    if os.path.exists(ptyexec_path):
        cmd = [ptyexec_path]
    else:
        # fall back to /usr/bin/script
        # this is not preferred because it uses way too much CPU
        # checking stdin for input that will never come...
        cmd = ['/usr/bin/script', '-q', '-t', '1', '/dev/null']
    cmd.append('/usr/sbin/softwareupdate')

    os_version_tuple = munkicommon.getOsVersion(as_tuple=True)
    if os_version_tuple > (10, 5):
        cmd.append('-v')

    cmd.extend(options_list)
    # bump up verboseness so we get download percentage done feedback.
    oldverbose = munkicommon.verbose
    munkicommon.verbose = oldverbose + 1

    try:
        job = launchd.Job(cmd)
        job.start()
    except launchd.LaunchdJobException, err:
        munkicommon.display_warning(
            'Error with launchd job (%s): %s', cmd, str(err))
        munkicommon.display_warning('Skipping softwareupdate run.')
        return -3

    results['installed'] = []
    results['download'] = []

    while True:
        if stop_allowed and munkicommon.munkistatusoutput:
            if munkicommon.stopRequested():
                job.stop()
                break

        output = job.stdout.readline()
        if not output:
            if job.returncode() is not None:
                break
            else:
                # no data, but we're still running
                # sleep a bit before checking for more output
                time.sleep(1)
                continue

        output = output.decode('UTF-8').strip()
        # send the output to STDOUT or MunkiStatus as applicable
        if output.startswith('Progress: '):
            # Snow Leopard/Lion progress info with '-v' flag
            try:
                percent = int(output[10:].rstrip('%'))
            except ValueError:
                percent = -1
            munkicommon.display_percent_done(percent, 100)
        elif output.startswith('Software Update Tool'):
            # don't display this
            pass
        elif output.startswith('Copyright 2'):
            # don't display this
            pass
        elif output.startswith('Installing ') and mode == 'install':
            item = output[11:]
            if item:
                if munkicommon.munkistatusoutput:
                    munkistatus.message(output)
                    munkistatus.detail('')
                    munkistatus.percent(-1)
                    munkicommon.log(output)
                else:
                    munkicommon.display_status(output)
        elif output.startswith('Installed '):
            # 10.6 / 10.7. Successful install of package name.
            if mode == 'install':
                munkicommon.display_status(output)
                results['installed'].append(output[10:])
            else:
                pass
                # don't display.
                # softwareupdate logging "Installed" at the end of a
                # successful download-only session is odd.
        elif output.startswith('Done '):
            # 10.5. Successful install of package name.
            munkicommon.display_status(output)
            results['installed'].append(output[5:])
        elif output.startswith('Downloading ') and mode == 'install':
            # This is 10.5 & 10.7 behaviour for an entirely missing subpackage.
            munkicommon.display_warning(
                'A necessary subpackage is not available on disk '
                'during an Apple Software Update installation '
                'run: %s' % output)
            results['download'].append(output[12:])
        elif output.startswith('Package failed:'):
            # Doesn't tell us which package.
            munkicommon.display_error(
                'Apple update failed to install: %s' % output)
        elif output.startswith('x '):
            # don't display this, it's just confusing
            pass
        elif 'Missing bundle identifier' in output:
            # don't display this, it's noise
            pass
        elif output == '':
            pass
        elif os_version_tuple == (10, 5) and output[0] in '.012468':
            # Leopard: See if there is percent-done info we can use,
            # which will look something like '.20..' or '0..' or '.40...60..'
            # so strip '.' chars and grab the last set of numbers
            output = output.strip('.').split('.')[-1]
            try:
                percent = int(output)
                if percent in [0, 20, 40, 60, 80, 100]:
                    munkicommon.display_percent_done(percent, 100)
            except ValueError:
                pass
        else:
            munkicommon.display_status(output)

    retcode = job.returncode()
    if retcode == 0:
        # get SoftwareUpdate's LastResultCode
        LastResultCode = getSoftwareUpdatePref('LastResultCode') or 0
        if LastResultCode > 2:
            retcode = LastResultCode

    # set verboseness back.
    munkicommon.verbose = oldverbose

    return retcode


def restartNeeded():
    '''Returns True if any update in AppleUpdates.plist requires an update.'''
    try:
        appleupdates = FoundationPlist.readPlist(appleUpdatesFile())
    except FoundationPlist.NSPropertyListSerializationException:
        return True
    for item in appleupdates.get('AppleUpdates', []):
        if item.get('RestartAction') in ['RequireRestart', 'RecommendRestart']:
            return True
    # if we get this far, there must be no items that require restart
    return False


def installAppleUpdates():
    '''Uses /usr/sbin/softwareupdate to install previously downloaded updates.

    Returns:
      Boolean. True if a restart is needed after install, False otherwise.'''
    msg = 'Installing available Apple Software Updates...'
    if munkicommon.munkistatusoutput:
        munkistatus.message(msg)
        munkistatus.detail('')
        munkistatus.percent(-1)
        munkicommon.log(msg)
    else:
        munkicommon.display_status(msg)
    restartneeded = restartNeeded()
    # use our filtered local catalog
    catalogpath = os.path.join(swupdCacheDir(), LOCAL_CATALOG_REL_PATH)

    if not os.path.exists(catalogpath):
        munkicommon.display_error(
            'Missing local Software Update catalog at %s', catalogpath)
        # didn't do anything, so no restart needed
        return False

    installlist = getSoftwareUpdateInfo()
    installresults = {'installed':[], 'download':[]}

    catalogURL = 'file://localhost' + urllib2.quote(catalogpath)
    retcode = run_softwareupdate(['--CatalogURL', catalogURL, '-i', '-a'],
                                 mode='install', results=installresults)

    if not 'InstallResults' in munkicommon.report:
        munkicommon.report['InstallResults'] = []

    for item in installlist:
        rep = {}
        rep['name'] = item.get('display_name')
        rep['version'] = item.get('version_to_install', '')
        rep['applesus'] = True
        rep['time'] = NSDate.new()
        rep['productKey'] = item.get('productKey', '')
        message = 'Apple Software Update install of %s-%s: %s'
        if rep['name'] in installresults['installed']:
            rep['status'] = 0
            install_status = 'SUCCESSFUL'
        elif rep['name'] in installresults['download']:
            rep['status'] = -1
            install_status = 'FAILED due to missing package.'
            munkicommon.display_warning(
                'Apple update %s, %s failed. A sub-package was missing '
                'on disk at time of install.'
                % (rep['name'], rep['productKey']))
        else:
            rep['status'] = -2
            install_status = 'FAILED for unknown reason'
            munkicommon.display_warning(
                'Apple update %s, %s failed to install. No record of '
                'success or failure.' % (rep['name'],rep['productKey']))

        munkicommon.report['InstallResults'].append(rep)
        log_msg = message % (rep['name'], rep['version'], install_status)
        munkicommon.log(log_msg, 'Install.log')

    if retcode:
        # there was an error
        munkicommon.display_error('softwareupdate error: %s' % retcode)
    # clean up our now stale local cache
    cachedir = os.path.join(swupdCacheDir())
    if os.path.exists(cachedir):
        unused_retcode = subprocess.call(['/bin/rm', '-rf', cachedir])
    # remove the now invalid appleUpdatesFile
    try:
        os.unlink(appleUpdatesFile())
    except OSError:
        pass
    # Also clear our pref value for last check date. We may have
    # just installed an update which is a pre-req for some other update.
    # Let's check again soon.
    munkicommon.set_pref('LastAppleSoftwareUpdateCheck', None)

    return restartneeded


#########################################################
###    Leopard-specific SoftwareUpdate workarounds    ###

def setupSoftwareUpdateCheck():
    '''Set defaults for root user and current host; needed for Leopard.'''
    defaults = {
        'AgreedToLicenseAgreement': True,
        'AutomaticDownload': True,
        'LaunchAppInBackground': True,
    }
    for key, value in defaults.iteritems():
        CFPreferencesSetValue(
            key, value, 'com.apple.SoftwareUpdate',
            kCFPreferencesCurrentUser, kCFPreferencesCurrentHost)
    if not CFPreferencesAppSynchronize('com.apple.SoftwareUpdate'):
        munkicommon.display_warning(
            'Error setting com.apple.SoftwareUpdate ByHost preferences')


def leopardDownloadAvailableUpdates(catalogURL):
    '''Clunky process to download Apple updates in Leopard.'''
    softwareupdateapp = '/System/Library/CoreServices/Software Update.app'
    softwareupdateappbin = os.path.join(
        softwareupdateapp, 'Contents/MacOS/Software Update')
    softwareupdatecheck = os.path.join(
        softwareupdateapp, 'Contents/Resources/SoftwareUpdateCheck')

    try:
        # record mode of Software Update.app executable
        rawmode = os.stat(softwareupdateappbin).st_mode
        oldmode = stat.S_IMODE(rawmode)
        # set mode of Software Update.app executable so it won't launch
        # yes, this is a hack.  So sue me.
        os.chmod(softwareupdateappbin, 0)
    except OSError, err:
        munkicommon.display_warning(
            'Error with os.stat(Softare Update.app): %s', str(err))
        munkicommon.display_warning('Skipping Apple SUS check.')
        return -2

    # Set SoftwareUpdateCheck to do things automatically
    setupSoftwareUpdateCheck()
    # switch to our local filtered sucatalog
    # Using the NSDefaults Argument Domain described here:
    # http://developer.apple.com/library/mac/#documentation/
    #        Cocoa/Conceptual/UserDefaults/Concepts/DefaultsDomains.html
    cmd = [softwareupdatecheck, '-CatalogURL', catalogURL]
    # bump up verboseness so we get download percentage done feedback.
    oldverbose = munkicommon.verbose
    munkicommon.verbose = oldverbose + 1

    try:
        # now check for updates
        proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError, err:
        munkicommon.display_warning('Error with Popen(%s): %s', cmd, str(err))
        munkicommon.display_warning('Skipping Apple SUS check.')
        # safely revert the chmod from above.
        try:
            # put mode back for Software Update.app executable
            os.chmod(softwareupdateappbin, oldmode)
        except OSError:
            pass
        return -3

    while True:
        output = proc.stdout.readline().decode('UTF-8')
        if (munkicommon.munkistatusoutput and
            munkicommon.stopRequested()):
            os.kill(proc.pid, 15) #15 is SIGTERM
            break
        if not output and (proc.poll() != None):
            break
        # send the output to STDOUT or MunkiStatus as applicable
        if output.rstrip() == '':
            continue
        # output from SoftwareUpdateCheck looks like this:
        # 2011-07-28 09:35:58.450 SoftwareUpdateCheck[598:10b] Downloading foo
        # We can pretty it up before display.
        fields = output.rstrip().split()
        if len(fields) > 3:
            munkicommon.display_status(' '.join(fields[3:]))

    retcode = proc.poll()
    # there's always an error on Leopard
    # because we prevent the app from launching
    # so let's just ignore them
    retcode = 0
    # get SoftwareUpdate's LastResultCode
    LastResultCode = getSoftwareUpdatePref('LastResultCode') or 0
    if LastResultCode > 2:
        retcode = LastResultCode
    if retcode:  # retcode != 0, error
        munkicommon.display_error('softwareupdate error: %s' % retcode)

    try:
        # put mode back for Software Update.app executable
        os.chmod(softwareupdateappbin, oldmode)
    except OSError:
        pass

    # set verboseness back.
    munkicommon.verbose = oldverbose
    return retcode

### End Leopard-specific workarounds ###
########################################


def downloadAvailableUpdates():
    '''Downloads the available Apple updates using our local filtered sucatalog.

    Returns:
      Boolean. True if successful, False otherwise.'''
    msg = 'Downloading available Apple Software Updates...'
    if munkicommon.munkistatusoutput:
        munkistatus.message(msg)
        munkistatus.detail('')
        munkistatus.percent(-1)
        munkicommon.log(msg)
    else:
        munkicommon.display_status(msg)

    # use our filtered local download catalog
    catalogpath = os.path.join(
        swupdCacheDir(), LOCAL_DOWNLOAD_CATALOG_REL_PATH)
    if not os.path.exists(catalogpath):
        munkicommon.display_error(
            'Missing local Software Update catalog at %s', catalogpath)
        return False

    catalogURL = 'file://localhost' + urllib2.quote(catalogpath)

    if munkicommon.getOsVersion() == '10.5':
        retcode = leopardDownloadAvailableUpdates(catalogURL)
    else:
        retcode = run_softwareupdate(['--CatalogURL', catalogURL, '-d', '-a'])

    if retcode:
        # there was an error
        munkicommon.display_error('softwareupdate error: %s' % retcode)
        return False
    return True


def getAvailableUpdates():
    '''Returns a list of product IDs of available Apple updates'''
    msg = 'Checking for available Apple Software Updates...'
    if munkicommon.munkistatusoutput:
        munkistatus.message(msg)
        munkistatus.detail('')
        munkistatus.percent(-1)
        munkicommon.log(msg)
    else:
        munkicommon.display_status(msg)

    applicable_updates = os.path.join(swupdCacheDir(), APPLICABLE_UPDATES)
    if os.path.exists(applicable_updates):
        # remove any old item
        try:
            os.unlink(applicable_updates)
        except (OSError, IOError):
            pass

    # use our locally-cached Apple catalog
    catalogpath = os.path.join(
        swupdCacheDir(), APPLE_EXTRACTED_CATALOG_REL_PATH)
    catalogURL = 'file://localhost' + urllib2.quote(catalogpath)
    su_options = ['--CatalogURL', catalogURL, '-l', '-f', applicable_updates]

    retcode = run_softwareupdate(su_options)
    if retcode:
        # there was an error
        if munkicommon.getOsVersion() == '10.5':
            pass  # Leopard softwareupdate always returns a non-zero exit code.
        else:
            munkicommon.display_error('softwareupdate error: %s' % retcode)
            return []

    try:
        updatelist = FoundationPlist.readPlist(applicable_updates)
    except FoundationPlist.NSPropertyListSerializationException:
        # plist either doesn't exist or is malformed.
        return []

    if not updatelist:
        return []

    results_array = updatelist.get('phaseResultsArray', [])
    return [item['productKey'] for item in results_array
            if 'productKey' in item]


def extractAppleSUScatalog():
    '''Copy the downloaded SUCatalog to a new catalog, extracting if gzipped.'''
    local_apple_sus_catalog_dir = os.path.join(
        swupdCacheDir(), LOCAL_CATALOG_DIR_REL_PATH)
    if not os.path.exists(local_apple_sus_catalog_dir):
        try:
            os.makedirs(local_apple_sus_catalog_dir)
        except OSError, oserr:
            raise ReplicationError(oserr)

    download_location = os.path.join(
        swupdCacheDir(temp=False), APPLE_DOWNLOAD_CATALOG_NAME)
    local_apple_sus_catalog = os.path.join(
        local_apple_sus_catalog_dir, APPLE_EXTRACTED_CATALOG_NAME)
    f = open(download_location, 'rb')
    magic = f.read(2)
    f.close()
    contents = ''
    if magic == '\x1f\x8b':
        # File is gzip compressed.
        f = gzip.open(download_location, 'rb')
    else:
        # Hopefully a nice plain plist.
        f = open(download_location, 'rb')
    contents = f.read()
    f.close()
    f = open(local_apple_sus_catalog, 'wb')
    f.write(contents)
    f.close()


def cacheAppleSUScatalog():
    '''Caches a local copy of the current Apple SUS catalog.'''
    munkisuscatalog = munkicommon.pref('SoftwareUpdateServerURL')
    prefs_catalogURL = getSoftwareUpdatePref('CatalogURL')
    if munkisuscatalog:
        # defined in Munki's prefs? use that
        catalogURL = munkisuscatalog
    elif prefs_catalogURL:
        # defined via MCX or
        # in /Library/Preferences/com.apple.SoftwareUpdate.plist
        catalogURL = prefs_catalogURL
    else:
        os_version = munkicommon.getOsVersion()
        catalogURL = DEFAULT_CATALOG_URLS.get(os_version, None)
        if not catalogURL:
            munkicommon.display_error(
                'No default Software Update CatalogURL for: %s', os_version)
            return -1
    if not os.path.exists(swupdCacheDir(temp=False)):
        try:
            os.makedirs(swupdCacheDir(temp=False))
        except OSError, oserr:
            raise ReplicationError(oserr)
    munkicommon.display_detail('Caching CatalogURL %s', catalogURL)
    download_location = os.path.join(
        swupdCacheDir(temp=False), APPLE_DOWNLOAD_CATALOG_NAME)
    try:
        file_changed = updatecheck.getResourceIfChangedAtomically(
            catalogURL, download_location, resume=True)
        extractAppleSUScatalog()
        return file_changed
    except updatecheck.MunkiDownloadError:
        return -1


def installedApplePackagesChanged():
    '''Generates a SHA-256 checksum of the info for all packages in the
    receipts database whose id matches com.apple.* and compares it to
    a stored version of this checksum.

    Returns:
      Boolean. False if the checksums match, True if they differ.'''
    cmd = ['/usr/sbin/pkgutil', '--regexp', '-pkg-info-plist',
           'com\.apple\.*']
    proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, unused_err) = proc.communicate()
    current_apple_packages_checksum = hashlib.sha256(output).hexdigest()
    old_apple_packages_checksum = munkicommon.pref(
        'InstalledApplePackagesChecksum')
    if current_apple_packages_checksum == old_apple_packages_checksum:
        return False
    else:
        munkicommon.set_pref('InstalledApplePackagesChecksum',
                             current_apple_packages_checksum)
        return True


def checkForSoftwareUpdates(forcecheck=True):
    '''Does our Apple Software Update check if needed.'''
    sucatalog = os.path.join(
        swupdCacheDir(temp=False), APPLE_DOWNLOAD_CATALOG_NAME)
    catcksum = munkicommon.getsha256hash(sucatalog)
    try:
        catalogchanged = cacheAppleSUScatalog()
    except ReplicationError, err:
        munkicommon.display_warning('Could not download Apple SUS catalog:')
        munkicommon.display_warning('\t', err)
        return False
    if catalogchanged == -1:
        munkicommon.display_warning('Could not download Apple SUS catalog.')
        return False
    if catalogchanged and catcksum != munkicommon.getsha256hash(sucatalog):
        munkicommon.log('Apple update catalog has changed.')
        forcecheck = True
    if installedApplePackagesChanged():
        munkicommon.log('Installed Apple packages have changed.')
        forcecheck = True
    if not availableUpdatesAreDownloaded():
        munkicommon.log('Downloaded updates do not match our list '
                        'of available updates.')
        forcecheck = True

    if not forcecheck:
        munkicommon.log('Skipping Apple Software Update check because '
                        'sucatalog is unchanged, installed Apple packages '
                        'are unchanged and we recently did a full check')
        return False

    updatelist = getAvailableUpdates()
    if not updatelist:
        # No updates found (not currently differentiating
        # "softwareupdate -l" failure from no updates found).
        munkicommon.set_pref('LastAppleSoftwareUpdateCheck',
                             NSDate.date())
        return False

    writeFilteredUpdateCatalog(updatelist)
    try:
        cacheSwupdMetadata()
    except ReplicationError, err:
        munkicommon.display_warning(
            'Could not replicate software update metadata:')
        munkicommon.display_warning('\t', err)
        return False
    if downloadAvailableUpdates():
        # Download success. Updates ready to install.
        munkicommon.set_pref('LastAppleSoftwareUpdateCheck',
                             NSDate.date())
        return True
    else:
        # Download error, allow check again soon.
        return False


def availableUpdatesAreDownloaded():
    '''Verifies that applicable/available Apple updates have been downloaded.

    Returns:
      Boolean. False if one or more product directories are missing, True
      otherwise (including when there are no available updates).'''
    appleUpdates = getSoftwareUpdateInfo()
    if not appleUpdates:
        return True

    try:
        downloadIndex = FoundationPlist.readPlist(
            '/Library/Updates/index.plist')
        downloaded = downloadIndex.get('ProductPaths', [])
    except FoundationPlist.FoundationPlistException:
        munkicommon.log('Apple downloaded update index is invalid. '
                        '/Library/Updates/index.plist')
        return False

    for update in appleUpdates:
        productKey = update.get('productKey')
        if productKey:
            product_dir_exists = os.path.isdir(os.path.join(
                '/Library/Updates', downloaded.get(productKey, '')))
            if productKey not in downloaded:
                munkicommon.log('Apple Update product not downloaded: %s' %
                                update['name'])
                return False
            elif not product_dir_exists:
                munkicommon.log('Apple Update product directory missing: %s' %
                                update['name'])
                return False
    return True


def getSoftwareUpdatePref(prefname):
    '''Returns a preference from the 
    com.apple.SoftwareUpdate preferences domain.

    Uses CoreFoundation methods.'''
    return CFPreferencesCopyAppValue(prefname, 'com.apple.SoftwareUpdate')


def getSoftwareUpdateInfo():
    '''Uses AvailableUpdates.plist to generate the AppleUpdates.plist,
    which records available updates in the format Managed Software Update.app
    expects.'''
    applicable_updates = os.path.join(swupdCacheDir(), APPLICABLE_UPDATES)
    if not os.path.exists(applicable_updates):
        # no applicable_updates, so bail
        return []

    infoarray = []
    plist = FoundationPlist.readPlist(applicable_updates)
    update_list = plist.get('phaseResultsArray', [])
    for update in update_list:
        iteminfo = {}
        iteminfo['description'] = update.get('description', '')
        iteminfo['name'] = update['ignoreKey']
        iteminfo['version_to_install'] = update['version']
        iteminfo['display_name'] = update['name']
        iteminfo['installed_size'] = update['sizeInKB']
        if update.get('restartRequired') == 'YES':
            iteminfo['RestartAction'] = 'RequireRestart'
        iteminfo['productKey'] = update['productKey']
        infoarray.append(iteminfo)
    return infoarray


def writeAppleUpdatesFile():
    '''Writes a file used by Managed Software Update.app to display available
    updates.'''
    appleUpdates = getSoftwareUpdateInfo()
    if appleUpdates:
        plist = {}
        plist['AppleUpdates'] = appleUpdates
        FoundationPlist.writePlist(plist, appleUpdatesFile())
        return True
    else:
        try:
            os.unlink(appleUpdatesFile())
        except (OSError, IOError):
            pass
        return False


def displayAppleUpdateInfo():
    '''Prints Apple update information.'''
    try:
        updatelist = FoundationPlist.readPlist(appleUpdatesFile())
    except FoundationPlist.FoundationPlistException:
        return
    appleupdates = updatelist.get('AppleUpdates', [])
    if len(appleupdates):
        munkicommon.display_info(
            'The following Apple Software Updates are available to install:')
    for item in appleupdates:
        munkicommon.display_info('    + %s-%s' % (
            item.get('display_name', ''), item.get('version_to_install', '')))
        if item.get('RestartAction') in ['RequireRestart', 'RecommendRestart']:
            munkicommon.display_info('       *Restart required')
            munkicommon.report['RestartRequired'] = True
        if item.get('RestartAction') == 'RequireLogout':
            munkicommon.display_info('       *Logout required')
            munkicommon.report['LogoutRequired'] = True


def appleSoftwareUpdatesAvailable(forcecheck=False, suppresscheck=False):
    '''Checks for available Apple Software Updates, trying not to hit the SUS
    more than needed.'''
    if suppresscheck:
        # typically because we're doing a logout install; if
        # there are no waiting Apple Updates we shouldn't
        # trigger a check for them.
        pass
    elif forcecheck:
        # typically because user initiated the check from
        # Managed Software Update.app
        unused_retcode = checkForSoftwareUpdates(forcecheck=True)
    else:
        # have we checked recently?  Don't want to check with
        # Apple Software Update server too frequently
        now = NSDate.new()
        nextSUcheck = now
        lastSUcheckString = munkicommon.pref('LastAppleSoftwareUpdateCheck')
        if lastSUcheckString:
            try:
                lastSUcheck = NSDate.dateWithString_(lastSUcheckString)
                interval = 24 * 60 * 60
                nextSUcheck = lastSUcheck.dateByAddingTimeInterval_(interval)
            except (ValueError, TypeError):
                pass
        if now.timeIntervalSinceDate_(nextSUcheck) >= 0:
            unused_retcode = checkForSoftwareUpdates(forcecheck=True)
        else:
            unused_retcode = checkForSoftwareUpdates(forcecheck=False)

    if writeAppleUpdatesFile():
        displayAppleUpdateInfo()
        return True
    else:
        return False


def clearAppleUpdateInfo():
    '''Clears Apple update info.

    This is called after performing munki updates because the Apple updates may
    no longer be relevant.'''
    try:
        os.unlink(appleUpdatesFile())
    except (OSError, IOError):
        pass


CACHEDUPDATELIST = None
def softwareUpdateList():
    '''Returns a list of available updates using /usr/sbin/softwareupdate -l'''
    global CACHEDUPDATELIST
    if CACHEDUPDATELIST != None:
        return CACHEDUPDATELIST

    updates = []
    munkicommon.display_detail(
        'Getting list of available Apple Software Updates')
    cmd = ['/usr/sbin/softwareupdate', '-l']
    proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                           stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, unused_err) = proc.communicate()
    if proc.returncode == 0:
        updates = [str(item)[5:] for item in str(output).splitlines()
                       if str(item).startswith('   * ')]
    munkicommon.display_detail(
        'softwareupdate returned %s updates' % len(updates))
    CACHEDUPDATELIST = updates
    return CACHEDUPDATELIST


def appleUpdatesFile():
    '''Returns path to the AppleUpdates.plist.'''
    return os.path.join(
        munkicommon.pref('ManagedInstallDir'), 'AppleUpdates.plist')
