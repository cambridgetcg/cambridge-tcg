"""Trade-in email templates — plain text for better deliverability."""


def confirmation_email(submission):
    """Build confirmation email after trade-in submission."""
    chosen_total = (submission['quoted_credit_total']
                    if submission['payment_method'] == 'credit'
                    else submission['quoted_cash_total'])
    payment_label = ('Store Credit' if submission['payment_method'] == 'credit'
                     else 'Cash (bank transfer)')
    delivery_label = ('Mail-in' if submission['delivery_method'] == 'mail'
                      else 'In-store drop-off')

    subject = 'Trade-In Received: %s' % submission['reference']

    body = (
        'Hi %s,\n\n'
        'Thank you for your trade-in submission. Here are your details:\n\n'
        'Reference: %s\n'
        'Items: %d cards\n'
        'Payment Method: %s\n'
        'Quoted Total: \u00a3%.2f\n'
        'Quote Valid Until: %s\n\n'
    ) % (
        submission['customer_name'],
        submission['reference'],
        submission['item_count'],
        payment_label,
        chosen_total,
        submission['expires_at'],
    )

    if submission['delivery_method'] == 'mail':
        body += (
            'NEXT STEPS:\n'
            '1. Pack your cards carefully (sleeved, in toploaders, rigid mailer)\n'
            '2. Ship to: Cambridge TCG, Cambridge, UK\n'
            '3. Use tracked delivery (Royal Mail Tracked 48 recommended)\n'
            '4. Email your tracking number to contact@cambridgetcg.com\n'
            '   with reference %s\n'
            '5. Cards must arrive by %s (7-day price lock)\n\n'
        ) % (submission['reference'], submission['expires_at'])
    else:
        body += (
            'NEXT STEPS:\n'
            '1. Bring your cards to our Cambridge shop\n'
            '2. Quote your reference: %s\n'
            '3. We\'ll verify and process on the spot\n\n'
        ) % submission['reference']

    body += (
        'You can check your trade-in status at any time:\n'
        'https://tradein.cambridgetcg.com/#/status\n\n'
        'Questions? Email us at contact@cambridgetcg.com\n\n'
        'Cambridge TCG\n'
    )

    return subject, body


def received_email(submission):
    """Build email notifying customer their cards have been received."""
    chosen_total = (submission['quoted_credit_total']
                    if submission['payment_method'] == 'credit'
                    else submission['quoted_cash_total'])
    payment_label = ('Store Credit' if submission['payment_method'] == 'credit'
                     else 'Cash (bank transfer)')

    subject = 'Cards Received: %s' % submission['reference']

    body = (
        'Hi %s,\n\n'
        'We\'ve received your cards for trade-in %s.\n\n'
        'We\'re now inspecting your cards against the submitted list. '
        'You\'ll receive another email once payment has been issued.\n\n'
        'Reminder:\n'
        '  Payment Method: %s\n'
        '  Quoted Total: \u00a3%.2f\n\n'
        'Check your status: https://tradein.cambridgetcg.com/#/status\n\n'
        'Cambridge TCG\n'
    ) % (
        submission['customer_name'],
        submission['reference'],
        payment_label,
        chosen_total,
    )

    return subject, body


def payment_email(submission):
    """Build email notifying customer that payment has been issued."""
    chosen_total = (submission['quoted_credit_total']
                    if submission['payment_method'] == 'credit'
                    else submission['quoted_cash_total'])
    payment_label = ('Store Credit' if submission['payment_method'] == 'credit'
                     else 'Cash (bank transfer)')

    subject = 'Payment Issued: %s' % submission['reference']

    body = (
        'Hi %s,\n\n'
        'Payment has been issued for your trade-in %s.\n\n'
        'Payment Method: %s\n'
        'Amount: \u00a3%.2f\n'
    ) % (
        submission['customer_name'],
        submission['reference'],
        payment_label,
        chosen_total,
    )

    if submission.get('payment_reference'):
        body += 'Payment Reference: %s\n' % submission['payment_reference']

    if submission['payment_method'] == 'credit':
        body += (
            '\nYour store credit discount code has been sent '
            'to this email address.\n'
        )
    else:
        body += (
            '\nThe bank transfer has been initiated and should arrive '
            'within 1-2 business days.\n'
        )

    body += '\nThank you for trading with us!\n\nCambridge TCG\n'

    return subject, body
