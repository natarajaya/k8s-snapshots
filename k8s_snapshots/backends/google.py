import json
import pendulum
import re
from typing import List, Dict, NamedTuple
from googleapiclient import discovery
from oauth2client.service_account import ServiceAccountCredentials
from oauth2client.client import GoogleCredentials
import pykube.objects
import structlog
from k8s_snapshots.context import Context
from .abstract import Snapshot, SnapshotStatus, DiskIdentifier, NewSnapshotIdentifier
from ..errors import SnapshotCreateError, UnsupportedVolume


_logger = structlog.get_logger(__name__)


#: The regex that a snapshot name has to match.
#: Regex provided by the createSnapshot error response.
GOOGLE_SNAPSHOT_NAME_REGEX = r'^(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$'

# Google Label keys and values must conform to the following restrictions:
# - Keys and values cannot be longer than 63 characters each.
# - Keys and values can only contain lowercase letters, numeric characters,
#   underscores, and dashes. International characters are allowed.
# - Label keys must start with a lowercase letter and international characters
#   are allowed.
# - Label keys cannot be empty.
# See https://cloud.google.com/compute/docs/labeling-resources for more

#: The regex that a label key and value has to match, additionally it has to be
#: lowercase, this is checked with str().islower()
GOOGLE_LABEL_REGEX = r'^(?:[-\w]{0,63})$'


def validate_config(config):
    """Ensure the config of this backend is correct.
    """

    # XXX: check the gcloud_project key

    test_datetime = pendulum.now('utc').format(
        config['snapshot_datetime_format'])
    test_snapshot_name = f'dummy-snapshot-{test_datetime}'

    if not re.match(GOOGLE_SNAPSHOT_NAME_REGEX, test_snapshot_name):
        _logger.error(
            'config.error',
            key='snapshot_datetime_format',
            message='Snapshot datetime format returns invalid string. '
                    'Note that uppercase characters are forbidden.',
            test_snapshot_name=test_snapshot_name,
            regex=GOOGLE_SNAPSHOT_NAME_REGEX
        )
        is_valid = False

    # Configuration keys that are either a Google
    glabel_key_keys = {'snapshot_author_label'}
    glabel_value_keys = {'snapshot_author_label_key'}

    for key in glabel_key_keys | glabel_value_keys:
        value = config[key]  # type: str
        re_match = re.match(GOOGLE_LABEL_REGEX, value)
        is_glabel_key = key in glabel_key_keys
        is_glabel_valid = (
            re_match and value.islower() and
            value[0].isalpha() or not is_glabel_key
        )

        if not is_glabel_valid:
            _logger.error(
                'config.error',
                message=f'Configuration value is not a valid '
                        f'Google Label {"Key" if is_glabel_key else "Value"}. '
                        f'See '
                        f'https://cloud.google.com/compute/docs/labeling-resources '
                        f'for more',
                key_hints=['value', 'regex'],
                key=key,
                is_lower=value.islower(),
                value=config[key],
                regex=GOOGLE_LABEL_REGEX,
            )
            is_valid = False

    return is_valid


class GoogleDiskIdentifier(NamedTuple):
    name: str
    zone: str


def get_disk_identifier(volume: pykube.objects.PersistentVolume) -> GoogleDiskIdentifier:
    gce_disk = volume.obj['spec']['gcePersistentDisk']['pdName']

    # How can we know the zone? In theory, the storage class can
    # specify a zone; but if not specified there, K8s can choose a
    # random zone within the master region. So we really can't trust
    # that value anyway.
    # There is a label that gives a failure region, but labels aren't
    # really a trustworthy source for this.
    # Apparently, this is a thing in the Kubernetes source too, see:
    # getDiskByNameUnknownZone in pkg/cloudprovider/providers/gce/gce.go,
    # e.g. https://github.com/jsafrane/kubernetes/blob/2e26019629b5974b9a311a9f07b7eac8c1396875/pkg/cloudprovider/providers/gce/gce.go#L2455
    gce_disk_zone = volume.labels.get(
        'failure-domain.beta.kubernetes.io/zone'
    )
    if not gce_disk_zone:
        raise UnsupportedVolume('cannot find the zone of the disk')

    return GoogleDiskIdentifier(name=gce_disk, zone=gce_disk_zone)
    

def supports_volume(volume: pykube.objects.PersistentVolume):
    provisioner = volume.annotations.get('pv.kubernetes.io/provisioned-by')
    return provisioner == 'kubernetes.io/gce-pd'


def parse_timestamp(date_str: str) -> pendulum.Pendulum:
    return pendulum.parse(date_str).in_timezone('utc')


def snapshot_list_filter_expr(label_filters: Dict[str, str]) -> str:
    key = list(label_filters.keys())[0]
    value = label_filters[key]
    return f'labels.{key} eq {value}'


def load_snapshots(ctx, label_filters: Dict[str, str]) -> List[Snapshot]:
    """
    Return the existing snapshots.
    """
    resp = get_gcloud(ctx).snapshots().list(
        project=ctx.config['gcloud_project'],
        filter=snapshot_list_filter_expr(label_filters),
    ).execute()

    snapshots = []
    for item in resp.get('items', []):
        # We got to parse out the disk zone and name from the source disk.
        # It's an url that ends with '/zones/{zone}/disks/{name}'/
        _, zone, _, disk = item['sourceDisk'].split('/')[-4:]

        snapshots.append(Snapshot(
            name=item['name'],
            created_at=parse_timestamp(item['creationTimestamp']),
            disk=GoogleDiskIdentifier(zone=zone, name=disk)
        ))

    return snapshots


def create_snapshot(
    ctx: Context,
    disk: GoogleDiskIdentifier,
    snapshot_name: str,
    snapshot_description: str
) -> NewSnapshotIdentifier:
    request_body = {
        'name': snapshot_name,
        'description': snapshot_description
    }
    
    gcloud = get_gcloud(ctx)

    # Returns a ZoneOperation: {kind: 'compute#operation',
    # operationType: 'createSnapshot', ...}.
    # Google's documentation is confusing regarding this, since there's two
    # tables of payload parameter descriptions on the page, one of them
    # describes the input parameters, but contains output-only parameters,
    # the correct table can be found at
    # https://cloud.google.com/compute/docs/reference/latest/disks/createSnapshot#response
    operation = gcloud.disks().createSnapshot(
        disk=disk.name,
        project=ctx.config['gcloud_project'],
        zone=disk.zone,
        body=request_body
    ).execute()

    return {
        'snapshot_name': snapshot_name,
        'zone': disk.zone,
        'operation_name': operation['name']
    }


def get_snapshot_status(
    ctx: Context,
    snapshot_identifier: NewSnapshotIdentifier
) -> SnapshotStatus:
    """In Google Cloud, the createSnapshot operation returns a ZoneOperation
    object which goes from PENDING, to RUNNING, to DONE.
    The snapshot object itself can be CREATING, DELETING, FAILED, READY,
    or UPLOADING.

    We check both states to make sure the snapshot was created.
    """

    _log = _logger.new(
        snapshot_identifier=snapshot_identifier,
    )
    
    gcloud = get_gcloud(ctx)
    
    # First, check the operation state
    operation = gcloud.zoneOperations().get(
        project=ctx.config['gcloud_project'],
        zone=snapshot_identifier['zone'],
        operation=snapshot_identifier['operation_name']
    ).execute()

    if not operation['status'] == 'DONE':
        _log.debug('google.status.operation_not_complete',
                   status=operation['status'])
        return SnapshotStatus.PENDING

    # To be sure, check the state of the snapshot itself
    snapshot = gcloud.snapshots().get(
        snapshot=snapshot_identifier['snapshot_name'],
        project=ctx.config['gcloud_project']
    ).execute()

    status = snapshot['status']
    if status == 'FAILED':
        _log.debug('google.status.failed',
                   status=status)
        raise SnapshotCreateError(status)
    elif status != 'READY':
        _log.debug('google.status.not_ready',
                   status=status)
        return SnapshotStatus.PENDING

    return SnapshotStatus.COMPLETE


def set_snapshot_labels(
    ctx: Context,
    snapshot_identifier: NewSnapshotIdentifier,
    labels: Dict
):
    gcloud = get_gcloud(ctx)

    snapshot = gcloud.snapshots().get(
        snapshot=snapshot_identifier['snapshot_name'],
        project=ctx.config['gcloud_project']
    ).execute()
    
    body = {
        'labels': labels,
        'labelFingerprint': snapshot['labelFingerprint'],
    }
    return gcloud.snapshots().setLabels(
        resource=snapshot_identifier['snapshot_name'],
        project=ctx.config['gcloud_project'],
        body=body,
    ).execute()


def delete_snapshot(
    ctx: Context,
    snapshot: Snapshot
):
    gcloud = get_gcloud(ctx)
    return gcloud.snapshots().delete(
        snapshot=snapshot.name,
        project=ctx.config['gcloud_project']
    ).execute()


def get_gcloud(ctx, version: str= 'v1'):
    """
    Get a configured Google Compute API Client instance.

    Note that the Google API Client is not threadsafe. Cache the instance locally
    if you want to avoid OAuth overhead between calls.

    Parameters
    ----------
    version
        Compute API version
    """
    SCOPES = 'https://www.googleapis.com/auth/compute'
    credentials = None

    if ctx.config.get('gcloud_json_keyfile_name'):
        credentials = ServiceAccountCredentials.from_json_keyfile_name(
            ctx.config.get('gcloud_json_keyfile_name'),
            scopes=SCOPES)

    if ctx.config.get('gcloud_json_keyfile_string'):
        keyfile = json.loads(ctx.config.get('gcloud_json_keyfile_string'))
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            keyfile, scopes=SCOPES)

    if not credentials:
        credentials = GoogleCredentials.get_application_default()

    if not credentials:
        raise RuntimeError("Auth for Google Cloud was not configured")

    compute = discovery.build(
        'compute',
        version,
        credentials=credentials,
        # https://github.com/google/google-api-python-client/issues/299#issuecomment-268915510
        cache_discovery=False
    )
    return compute