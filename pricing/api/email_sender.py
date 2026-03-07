"""SES email sender — best-effort, never raises."""

import os

SES_ENABLED = os.environ.get('SES_ENABLED', 'false').lower() == 'true'
SES_SANDBOX_MODE = os.environ.get('SES_SANDBOX_MODE', 'true').lower() == 'true'
SENDER = 'Cambridge TCG <no-reply@cambridgetcg.com>'
STORE_EMAIL = 'contact@cambridgetcg.com'


def send_email(to, subject, body):
    """Send email via SES. Best-effort — never raises."""
    if not SES_ENABLED:
        print('SES disabled — would send to %s: %s' % (to, subject))
        return False

    try:
        import boto3
        ses = boto3.client('ses', region_name='us-east-1')

        destination = {'ToAddresses': [to]}
        if SES_SANDBOX_MODE:
            destination['CcAddresses'] = [STORE_EMAIL]

        ses.send_email(
            Source=SENDER,
            Destination=destination,
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}},
            },
        )
        print('Email sent to %s: %s' % (to, subject))
        return True
    except Exception as e:
        print('Email send failed: %s' % str(e))
        return False
