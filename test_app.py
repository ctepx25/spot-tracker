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
        self.assertIn("New Track Point Detected", args[1])  # Alert title

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
        self.assertIn("New Message Received", args[1])
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


if __name__ == "__main__":
    unittest.main()
