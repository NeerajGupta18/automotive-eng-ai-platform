import { Link } from 'react-router-dom'

export default function Privacy() {
  return (
    <div className="login" style={{ maxWidth: 640 }}>
      <Link to="/">Back</Link>
      <h1 style={{ marginTop: 18 }}>How CarSaathi handles your data</h1>
      <p><strong>What we store.</strong> Your email address and name, an optional mobile number, the cars you add (registration number,
        make, model and the details you enter), the documents and photos you upload, your reminders, and who you share a car with.
        We also keep a short record of sign-ins, sign-outs, exports and consent.</p>
      <p><strong>Why.</strong> Only to show this information to you and to the people you share a car with, and to work out what is
        due. We do not sell it, show ads, or share it with insurers, dealers or anyone else.</p>
      <p><strong>Help with car problems.</strong> The help search reads your description to find a guide; it isn't stored. Which guide
        helped (or didn't) is counted without your name or car. On servers that use an AI model to match descriptions, the text is
        sent to it with emails, phone numbers, number plates and VINs removed. Help requests, with their steps and photos, are visible only to you.
        A report to the car manufacturer is sent only if you tick the consent box for that report, and only with what the app shows
        you first: the car model, the date, what the car did, where and how fast, and the VIN or software version if you add them.
        Never your name, email, phone, number plate, your own description or photos.</p>
      <p><strong>Running costs and car health.</strong> The fills, charges and expenses you log (with any bill you photograph), the
        odometer readings that come with them, and the readings from an OBD-II adapter: fault codes, battery voltage, engine
        temperature and the adapter's raw answers. They belong to the car: everyone you share the car with sees them. The adapter
        only reads; CarSaathi never clears codes or changes anything in the car. None of this is sent to anyone else.</p>
      <p><strong>Notifications.</strong> Your inbox of reminders and booking updates, your notification settings, and for each
        browser where you turn notifications on, the address its push service gave it. Reminder emails go to your sign-in address
        through our email provider, at most one a day plus booking updates, and every email has a link that turns them off. Push
        messages are encrypted for your browser before they leave us; the push service (Google, Mozilla or Apple, depending on the
        browser) only passes them on. The Android app keeps the coming weeks' reminders on the phone to show them on time.</p>
      <p><strong>Service bookings.</strong> The workshops you save, and your bookings with their updates. When you send a booking,
        the workshop gets a private link that shows only that booking: the car's make, model, number and km, what needs doing, your
        preferred days, your first name, the mobile number you chose and your notes. Nothing else from your account. The link stops
        working when you turn it off, after 30 days, or a week after the booking ends. A bill you add becomes an expense of the car.</p>
      <p><strong>Who can see it.</strong> You, and the people you invite to a car (they see that car's details, documents and reminders).</p>
      <p><strong>How long.</strong> Until you delete it. Deleting your account removes your profile, your sign-ins and every car you are
        the only owner of, with its documents and reminders. Demo accounts are deleted automatically.</p>
      <p><strong>Your rights.</strong> Under India's Digital Personal Data Protection Act, 2023 you can see and download your data,
        correct it, and withdraw consent by deleting your account. All of this is in your profile.</p>
      <p><strong>Security.</strong> Sign-in uses one-time codes; we never store passwords or readable codes. Connections are encrypted.
        Uploaded files are only served to signed-in members of the car.</p>
      <p className="muted small">This notice describes a student project (the CarSaathi owner app). Version 2026-10, updated for help requests, running costs, car health, notifications and service bookings.</p>
    </div>
  )
}
