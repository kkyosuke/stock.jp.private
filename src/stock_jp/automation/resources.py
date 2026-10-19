"""Conservative history resource limits; existing codec caps remain unchanged."""
import os
import resource
import shutil

GiB=1024**3
MAX_RAW=2*GiB
MAX_STAGING=2*GiB
MIN_FREE=6*GiB
MAX_RESPONSE=16*1024**2
MAX_ADDRESS_SPACE=GiB


class ResourceLimit(ValueError):pass


def _tree_size(root):
    if not root.is_dir():
        return 0
    total = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                # DirEntry reuses type/stat information within this scan. Scan
                # again on every reservation; never cache a previous budget.
                if entry.is_file():
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
    return total


def inspect(source,staging):
    return {'raw_bytes':_tree_size(source),'staging_bytes':_tree_size(staging),'free_bytes':shutil.disk_usage(source).free,
            'max_raw_bytes':MAX_RAW,'max_staging_bytes':MAX_STAGING,'min_free_bytes':MIN_FREE,
            'max_response_bytes':MAX_RESPONSE,'max_address_space_bytes':MAX_ADDRESS_SPACE}


def enforce(report):
    if report['raw_bytes']+MAX_RESPONSE>MAX_RAW or report['staging_bytes']>MAX_STAGING or report['free_bytes']<MIN_FREE:
        raise ResourceLimit('history disk/raw/staging limit; no request sent')


def constrain_memory():
    soft,hard=resource.getrlimit(resource.RLIMIT_AS)
    ceiling=min([MAX_ADDRESS_SPACE]+[v for v in (soft,hard) if v!=resource.RLIM_INFINITY])
    resource.setrlimit(resource.RLIMIT_AS,(ceiling,hard))
