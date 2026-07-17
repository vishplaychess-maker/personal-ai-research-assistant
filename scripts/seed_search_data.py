"""
Phase 5C — Temporary development seed script for browser verification.

Creates test sessions and messages with searchable content to verify
the conversation search feature in the browser.

Usage:
    docker compose exec -T backend python /app/scripts/seed_search_data.py

This is a development-only script. Do not use in production.
The seed data will be visible in the database until manually removed
or the database is reset.
"""

import sys
import os

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from app.models.models import ResearchSession, Message, User


def seed():
    db = SessionLocal()
    try:
        # Ensure default user exists
        user = db.query(User).filter(User.id == 1).first()
        if not user:
            user = User(id=1, username="default", email="default@test.com")
            db.add(user)
            db.commit()
            print("Created default user.")

        # Clean up any previous seed data
        existing = (
            db.query(Message)
            .join(ResearchSession, ResearchSession.id == Message.session_id)
            .filter(ResearchSession.title.like("Seed: %"))
            .all()
        )
        if existing:
            print(f"Removing {len(existing)} existing seed messages...")
            for m in existing:
                db.delete(m)

        old_sessions = (
            db.query(ResearchSession)
            .filter(ResearchSession.title.like("Seed: %"))
            .all()
        )
        if old_sessions:
            for s in old_sessions:
                db.delete(s)
            db.commit()
            print(f"Removed {len(old_sessions)} old seed sessions.")

        # ── Session 1: Machine Learning ─────────────────────
        session1 = ResearchSession(title="Seed: Machine Learning Discussion", user_id=1)
        db.add(session1)
        db.flush()

        messages1 = [
            Message(
                session_id=session1.id,
                role="user",
                content="Can you explain what machine learning is?",
            ),
            Message(
                session_id=session1.id,
                role="assistant",
                content="Machine learning is a subset of artificial intelligence that enables systems to learn and improve from experience without being explicitly programmed.",
            ),
            Message(
                session_id=session1.id,
                role="user",
                content="What are the different types of machine learning?",
            ),
            Message(
                session_id=session1.id,
                role="assistant",
                content="The main types are supervised learning, unsupervised learning, and reinforcement learning. Supervised learning uses labeled data, unsupervised learning finds patterns in unlabeled data, and reinforcement learning uses rewards and punishments.",
            ),
            Message(
                session_id=session1.id,
                role="user",
                content="Can you give an example of supervised learning?",
            ),
            Message(
                session_id=session1.id,
                role="assistant",
                content="A classic example is email spam detection. You train a model on thousands of emails labeled 'spam' or 'not spam', and it learns to classify new emails automatically.",
            ),
        ]
        for m in messages1:
            db.add(m)

        # ── Session 2: Python Programming ──────────────────
        session2 = ResearchSession(title="Seed: Python Programming Help", user_id=1)
        db.add(session2)
        db.flush()

        messages2 = [
            Message(
                session_id=session2.id,
                role="user",
                content="How do I write a Python function that sorts a list?",
            ),
            Message(
                session_id=session2.id,
                role="assistant",
                content="You can use the built-in sorted() function or the list.sort() method. For example: sorted_list = sorted(my_list) returns a new sorted list, while my_list.sort() sorts in place.",
            ),
            Message(
                session_id=session2.id,
                role="user",
                content="What about sorting a list of dictionaries by a key?",
            ),
            Message(
                session_id=session2.id,
                role="assistant",
                content="Use sorted() with a key function: sorted(my_list, key=lambda x: x['name']). You can also use operator.itemgetter for better performance.",
            ),
            Message(
                session_id=session2.id,
                role="user",
                content="Can I sort in descending order?",
            ),
            Message(
                session_id=session2.id,
                role="assistant",
                content="Yes! Pass reverse=True: sorted(my_list, key=lambda x: x['name'], reverse=True). This works for both sorted() and .sort().",
            ),
        ]
        for m in messages2:
            db.add(m)

        # ── Session 3: Special Characters ──────────────────
        session3 = ResearchSession(title="Seed: C++ & Special Characters", user_id=1)
        db.add(session3)
        db.flush()

        messages3 = [
            Message(
                session_id=session3.id,
                role="user",
                content="How does C++ handle operator overloading?",
            ),
            Message(
                session_id=session3.id,
                role="assistant",
                content="C++ allows operator overloading where you define special member functions for operators like +, -, *, /. For example, you can define how two Matrix objects are added together.",
            ),
            Message(
                session_id=session3.id,
                role="user",
                content="What about C# and Java? Do they support it?",
            ),
            Message(
                session_id=session3.id,
                role="assistant",
                content="C# supports operator overloading similar to C++. Java does NOT support operator overloading for user-defined types. This was a deliberate design choice.",
            ),
            Message(
                session_id=session3.id,
                role="user",
                content="Can you show me a 100% code example?",
            ),
            Message(
                session_id=session3.id,
                role="assistant",
                content="```cpp\nclass Vector {\npublic:\n    int x, y;\n    Vector operator+(const Vector& other) {\n        return Vector{x + other.x, y + other.y};\n    }\n};\n```\nThis adds two Vector objects using the + operator.",
            ),
        ]
        for m in messages3:
            db.add(m)

        db.commit()
        print(f"Created 3 seed sessions with {len(messages1) + len(messages2) + len(messages3)} messages.")
        print()
        print("Searchable phrases:")
        print("  - \"machine learning\" (Session 1)")
        print("  - \"supervised learning\" (Session 1)")
        print("  - \"spam detection\" (Session 1)")
        print("  - \"Python function\" (Session 2)")
        print("  - \"sorted\" (Session 2)")
        print("  - \"descending order\" (Session 2)")
        print("  - \"C++\" (Session 3)")
        print("  - \"C#\" (Session 3)")
        print("  - \"operator overloading\" (Session 3)")
        print("  - \"Java\" (Session 3)")

    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
