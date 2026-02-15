"""add twitter demo tables

Revision ID: 41fcc492d07a
Revises:
Create Date: 2026-02-10 18:35:41.677475

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import advanced_alchemy.types


# revision identifiers, used by Alembic.
revision: str = '41fcc492d07a'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = ("twitter",)
depends_on: Union[str, Sequence[str], None] = ("skrift",)


def upgrade() -> None:
    op.create_table('tweets',
    sa.Column('id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('user_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('parent_id', advanced_alchemy.types.guid.GUID(length=16), nullable=True),
    sa.Column('retweet_of_id', advanced_alchemy.types.guid.GUID(length=16), nullable=True),
    sa.Column('like_count', sa.Integer(), nullable=False),
    sa.Column('reply_count', sa.Integer(), nullable=False),
    sa.Column('retweet_count', sa.Integer(), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), nullable=False),
    sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
    sa.Column('created_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.Column('updated_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['tweets.id'], name=op.f('fk_tweets_parent_id_tweets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['retweet_of_id'], ['tweets.id'], name=op.f('fk_tweets_retweet_of_id_tweets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_tweets_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tweets'))
    )
    op.create_index(op.f('ix_tweets_parent_id'), 'tweets', ['parent_id'], unique=False)
    op.create_index(op.f('ix_tweets_retweet_of_id'), 'tweets', ['retweet_of_id'], unique=False)
    op.create_index(op.f('ix_tweets_user_id'), 'tweets', ['user_id'], unique=False)

    op.create_table('follows',
    sa.Column('id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('follower_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('following_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
    sa.Column('created_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.Column('updated_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['follower_id'], ['users.id'], name=op.f('fk_follows_follower_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['following_id'], ['users.id'], name=op.f('fk_follows_following_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_follows')),
    sa.UniqueConstraint('follower_id', 'following_id', name='uq_follows_follower_following')
    )
    op.create_index(op.f('ix_follows_follower_id'), 'follows', ['follower_id'], unique=False)
    op.create_index(op.f('ix_follows_following_id'), 'follows', ['following_id'], unique=False)

    op.create_table('tweet_likes',
    sa.Column('id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('user_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('tweet_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
    sa.Column('created_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.Column('updated_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tweet_id'], ['tweets.id'], name=op.f('fk_tweet_likes_tweet_id_tweets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_tweet_likes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tweet_likes')),
    sa.UniqueConstraint('user_id', 'tweet_id', name='uq_tweet_likes_user_tweet')
    )
    op.create_index(op.f('ix_tweet_likes_tweet_id'), 'tweet_likes', ['tweet_id'], unique=False)
    op.create_index(op.f('ix_tweet_likes_user_id'), 'tweet_likes', ['user_id'], unique=False)

    op.create_table('bookmarks',
    sa.Column('id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('user_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('tweet_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
    sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
    sa.Column('created_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.Column('updated_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tweet_id'], ['tweets.id'], name=op.f('fk_bookmarks_tweet_id_tweets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_bookmarks_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bookmarks')),
    sa.UniqueConstraint('user_id', 'tweet_id', name='uq_bookmarks_user_tweet')
    )
    op.create_index(op.f('ix_bookmarks_tweet_id'), 'bookmarks', ['tweet_id'], unique=False)
    op.create_index(op.f('ix_bookmarks_user_id'), 'bookmarks', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bookmarks_user_id'), table_name='bookmarks')
    op.drop_index(op.f('ix_bookmarks_tweet_id'), table_name='bookmarks')
    op.drop_table('bookmarks')
    op.drop_index(op.f('ix_tweet_likes_user_id'), table_name='tweet_likes')
    op.drop_index(op.f('ix_tweet_likes_tweet_id'), table_name='tweet_likes')
    op.drop_table('tweet_likes')
    op.drop_index(op.f('ix_follows_following_id'), table_name='follows')
    op.drop_index(op.f('ix_follows_follower_id'), table_name='follows')
    op.drop_table('follows')
    op.drop_index(op.f('ix_tweets_user_id'), table_name='tweets')
    op.drop_index(op.f('ix_tweets_retweet_of_id'), table_name='tweets')
    op.drop_index(op.f('ix_tweets_parent_id'), table_name='tweets')
    op.drop_table('tweets')
