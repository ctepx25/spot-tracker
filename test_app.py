import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import os

# Set dummy environment variables for testing
os.environ["FLASK_ENV"] = "testing"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:ABCdef"
os.environ["TELEGRAM_CHAT_ID"] = "-987654321"

from app import app, db, Message, scrape_spot_feed, parse_date_to_unix_bounds


class SpotTrackerTestCase(unittest.TestCase):

    def setUp(self):
        # Configure app for testing
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()

        with app.app_context():
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.drop_all()

    def test_database_model(self):
        """Test that the Message model stores and retrieves data correctly."""
        with app.app_context():
            msg = Message(
                id=11111,
                messengerId="device-1",
                messengerName="John Tracker",
                modelId="SPOT4",
                messageType="TRACK",
                dateTime="2026-06-13T12:00:00+0000",
                unixTime=1781352000,
                latitude=34.56,
                longitude=45.67,
                altitude=150.0,
                batteryState="GOOD"
            )
            db.session.add(msg)
            db.session.commit()

            retrieved = db.session.get(Message, 11111)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.messengerName, "John Tracker")
            self.assertEqual(retrieved.latitude, 34.56)

    def test_parse_date_to_unix_bounds(self):
        """Test the YYYY-MM-DD to Unix bounds converter."""
        # 2026-06-13T00:00:00 UTC is 1781308800
        # 2026-06-13T23:59:59 UTC is 1781395199
        start, end = parse_date_to_unix_bounds("2026-06-13")
        self.assertEqual(start, 1781308800)
        self.assertEqual(end, 1781395199)

        # Handle bad formats gracefully
        start_bad, end_bad = parse_date_to_unix_bounds("invalid-date")
        self.assertIsNone(start_bad)
        self.assertIsNone(end_bad)

    @patch("requests.get")
    @patch("app.bot")
    def test_scraping_and_telegram_alerts(self, mock_bot, mock_get):
        """Test that new messages trigger database inserts and Telegram alerts."""
        # Mock SPOT API JSON response containing 1 TRACK message
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "count": 1,
                    "totalCount": 1,
                    "messages": {
                        "message": {
                            "id": 22222,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T09:48:29+0000",
                            "unixTime": 1781344109,
                            "latitude": 32.5806,
                            "longitude": 34.94616,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response

        # Call the scraping function
        scrape_spot_feed()

        # Check database persistence
        with app.app_context():
            saved_msg = db.session.get(Message, 22222)
            self.assertIsNotNone(saved_msg)
            self.assertEqual(saved_msg.messengerName, "Andrey")
            self.assertEqual(saved_msg.latitude, 32.5806)

        # Check that Telegram was notified for the new TRACK message
        self.assertTrue(mock_bot.send_message.called)
        args, kwargs = mock_bot.send_message.call_args
        self.assertEqual(args[0], "-987654321")  # Chat ID
        self.assertIn("Sattelite Tracker Started", args[1])  # Alert title

    @patch("requests.get")
    @patch("app.bot")
    def test_custom_user_message_telegram_alert(self, mock_bot, mock_get):
        """Test that CUSTOM messages trigger distinct Telegram alerts containing messageContent."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 33333,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "CUSTOM",
                            "dateTime": "2026-06-13T09:50:00+0000",
                            "unixTime": 1781344200,
                            "latitude": 32.581,
                            "longitude": 34.947,
                            "altitude": 12.0,
                            "batteryState": "GOOD",
                            "messageContent": "I reached the summit!"
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response

        scrape_spot_feed()

        # Verify notification contains custom content
        self.assertTrue(mock_bot.send_message.called)
        args, _ = mock_bot.send_message.call_args
        self.assertIn("Sattelite Tracker Message", args[1])
        self.assertIn("Device:", args[1])
        self.assertIn("Message:", args[1])
        self.assertIn("I reached the summit!", args[1])

    @patch("requests.get")
    @patch("app.bot")
    def test_duplicate_messages_ignored(self, mock_bot, mock_get):
        """Test that duplicate messages are not re-processed and do not trigger duplicate alerts."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 44444,
                            "messengerId": "dev-100",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T09:48:29+0000",
                            "unixTime": 1781344109,
                            "latitude": 32.5806,
                            "longitude": 34.94616,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response

        # First scrape (New Message)
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 1)

        # Reset bot mock call history
        mock_bot.send_message.reset_mock()

        # Second scrape (Duplicate Message)
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 0)  # No new alert sent!

    def test_index_route(self):
        """Test the dashboard index page responds successfully."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SPOT Satellite Tracker", response.data)

    def test_map_route(self):
        """Test that the map endpoint correctly renders folium elements."""
        response = self.client.get("/map?startDate=2026-06-13")
        self.assertEqual(response.status_code, 200)
        # Folium outputs Leaflet HTML, verify core Leaflet/Folium content exists
        self.assertIn(b"folium", response.data)
        self.assertIn(b"leaflet", response.data)

    @patch("requests.get")
    @patch("app.bot")
    def test_telegram_notifications_conditional_rules(self, mock_bot, mock_get):
        """Test conditional Telegram alerts: TRACK notified only if first of day or latest is custom."""
        # 1. First TRACK of the day -> should trigger alert
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 10001,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T08:00:00+0000",
                            "unixTime": 1781337600,
                            "latitude": 32.5801,
                            "longitude": 34.9461,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 1)
        self.assertIn("Sattelite Tracker Started", mock_bot.send_message.call_args[0][1])

        # Reset bot mock
        mock_bot.send_message.reset_mock()

        # 2. Second TRACK of the day (preceded by TRACK) -> should NOT trigger alert
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 10002,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T08:10:00+0000",
                            "unixTime": 1781338200,
                            "latitude": 32.5802,
                            "longitude": 34.9462,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 0)

        # 3. Custom (OK) message -> should trigger alert
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 10003,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "OK",
                            "dateTime": "2026-06-13T08:20:00+0000",
                            "unixTime": 1781338800,
                            "latitude": 32.5803,
                            "longitude": 34.9463,
                            "altitude": 10.0,
                            "batteryState": "GOOD",
                            "messageContent": "Everything OK!"
                        }
                    }
                }
            }
        }
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 1)
        self.assertIn("Message:", mock_bot.send_message.call_args[0][1])

        # Reset bot mock
        mock_bot.send_message.reset_mock()

        # 4. Third TRACK of the day (preceded by OK) -> should trigger alert
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 10004,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T08:30:00+0000",
                            "unixTime": 1781339400,
                            "latitude": 32.5804,
                            "longitude": 34.9464,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 1)
        self.assertIn("Sattelite Tracker Started", mock_bot.send_message.call_args[0][1])

        # Reset bot mock
        mock_bot.send_message.reset_mock()

        # 5. Fourth TRACK of the day (preceded by TRACK) -> should NOT trigger alert
        mock_response.json.return_value = {
            "response": {
                "feedMessageResponse": {
                    "messages": {
                        "message": {
                            "id": 10005,
                            "messengerId": "dev-100",
                            "messengerName": "Andrey",
                            "modelId": "SPOT2",
                            "messageType": "TRACK",
                            "dateTime": "2026-06-13T08:40:00+0000",
                            "unixTime": 1781340000,
                            "latitude": 32.5805,
                            "longitude": 34.9465,
                            "altitude": 10.0,
                            "batteryState": "GOOD"
                        }
                    }
                }
            }
        }
        scrape_spot_feed()
        self.assertEqual(mock_bot.send_message.call_count, 0)

    def test_index_route_displays_events_list(self):
        """Test that the index page displays all stored events for the selected date."""
        with app.app_context():
            # Add messages for 2026-06-13
            msg1 = Message(
                id=80001,
                messengerId="dev-100",
                messengerName="Andrey",
                modelId="SPOT2",
                messageType="TRACK",
                dateTime="2026-06-13T08:00:00+0000",
                unixTime=1781337600,
                latitude=32.5801,
                longitude=34.9461,
                altitude=10.0,
                batteryState="GOOD"
            )
            msg2 = Message(
                id=80002,
                messengerId="dev-100",
                messengerName="Andrey",
                modelId="SPOT2",
                messageType="OK",
                dateTime="2026-06-13T08:20:00+0000",
                unixTime=1781338800,
                latitude=32.5803,
                longitude=34.9463,
                altitude=10.0,
                batteryState="GOOD",
                messageContent="All good!"
            )
            db.session.add(msg1)
            db.session.add(msg2)
            db.session.commit()

        # Request index page for 2026-06-13
        response = self.client.get("/?startDate=2026-06-13")
        self.assertEqual(response.status_code, 200)
        # Ensure our table/list contains the events
        self.assertIn(b"Daily Events Log (2026-06-13)", response.data)
        self.assertIn(b"32.5801, 34.9461", response.data)
        self.assertIn(b"32.5803, 34.9463", response.data)
        self.assertIn(b"All good!", response.data)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        # Load real local environment variables from .env
        from dotenv import load_dotenv
        load_dotenv()

        # Import the real Flask app, db, and sender function
        from app import app, db, Message, send_telegram_alert, bot, TELEGRAM_CHAT_ID

        print("==================================================")
        print("🚀 Telegram Live Test Notification Dispatcher")
        print("==================================================")
        print(f"Chat ID: {TELEGRAM_CHAT_ID}")
        print(f"Bot Configured: {bool(bot)}")
        print("==================================================")

        if not bot or not TELEGRAM_CHAT_ID:
            print("❌ Error: Telegram bot token or chat ID not set in your .env file!")
            sys.exit(1)

        with app.app_context():
            # Build and send OK message using example data from the feed
            print("\n📬 1. Dispatching Test 'OK' Event...")
            ok_msg = Message(
                id=2457111472,
                messengerId="0-8335246",
                messengerName="Andrey",
                modelId="SPOT2",
                messageType="OK",
                dateTime="2026-06-16T14:49:36+0000",
                unixTime=1781621376,
                latitude=32.16047,
                longitude=34.83119,
                altitude=0.0,
                batteryState="GOOD",
                messageContent="Pilot 282 Andrey Badikov OK"
            )
            send_telegram_alert(ok_msg)

            # Build and send TRACK message using example data from the feed
            print("\n🛰️ 2. Dispatching Test 'TRACK' Event...")
            track_msg = Message(
                id=2457079320,
                messengerId="0-8335246",
                messengerName="Andrey",
                modelId="SPOT2",
                messageType="TRACK",
                dateTime="2026-06-16T13:38:58+0000",
                unixTime=1781617138,
                latitude=32.1572,
                longitude=34.83395,
                altitude=0.0,
                batteryState="GOOD"
            )
            send_telegram_alert(track_msg)

            print("\n✅ Test messages dispatched successfully! Check your Telegram chat.")
    else:
        unittest.main()
