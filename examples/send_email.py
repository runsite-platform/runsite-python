import runsite

sent = runsite.email.send(
    from_="Shop <orders@shop.com>",
    to="client@example.com",
    subject="Your order is on its way",
    html="<p>Thanks for your order!</p>",
)
print(sent.id, sent.status, "(sandbox)" if sent.is_sandbox else "")
