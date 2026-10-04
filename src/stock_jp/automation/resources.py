"""Conservative history resource limits; existing codec caps remain unchanged."""
import resource
import shutil

GiB=1024**3
MAX_RAW=2*GiB
MAX_STAGING=2*GiB
MIN_FREE=6*GiB
MAX_RESPONSE=16*1024**2
MAX_ADDRESS_SPACE=GiB


class ResourceLimit(ValueError):pass


def inspect(source,staging):
    def size(root):return sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) if root.exists() else 0
    return {'raw_bytes':size(source),'staging_bytes':size(staging),'free_bytes':shutil.disk_usage(source).free,
            'max_raw_bytes':MAX_RAW,'max_staging_bytes':MAX_STAGING,'min_free_bytes':MIN_FREE,
            'max_response_bytes':MAX_RESPONSE,'max_address_space_bytes':MAX_ADDRESS_SPACE}


def enforce(report):
    if report['raw_bytes']+MAX_RESPONSE>MAX_RAW or report['staging_bytes']>MAX_STAGING or report['free_bytes']<MIN_FREE:
        raise ResourceLimit('history disk/raw/staging limit; no request sent')


def constrain_memory():
    soft,hard=resource.getrlimit(resource.RLIMIT_AS)
    ceiling=min([MAX_ADDRESS_SPACE]+[v for v in (soft,hard) if v!=resource.RLIM_INFINITY])
    resource.setrlimit(resource.RLIMIT_AS,(ceiling,hard))
