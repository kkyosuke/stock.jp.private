"""Credential-name and integration readiness report; never makes network calls."""
import json


def report():
    return {
        'decision':'STOP_DISABLED',
        'workflow_activation_allowed':False,
        'credential_names':['AUTOMATION_PAT','JQUANTS_API_KEY'],
        'credential_validity':'UNVERIFIED',
        'operation_mode':'PAPER',
        'broker_submission':'HUMAN_ONLY',
        'driver':'actions_driver.py',
        'remaining_integration':['authenticated live controller/lease read transport; no receipt fallback'],
        'remaining_external_conditions':[
            'independent old scheduler stop and active-writer evidence',
            'reviewed pinned approval manifest and rejected-campaign resolution',
            'existing credential validity and minimum scope verification',
            'separate code review, activation and real-service acceptance',
        ],
    }


if __name__=='__main__':
    print(json.dumps(report(),sort_keys=True))
