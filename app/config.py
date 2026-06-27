from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    football_data_api_key: str = ""
    database_url: str = "sqlite:///./data/app.db"
    poll_interval_seconds: int = 300
    score_recheck_delay_seconds: int = 600
    admin_telegram_chat_id: str = ""
    debug: bool = False

    # Per-stage scoring: sign (1X2), goal difference, exact score
    # Defaults match the standard Porra Mundial scoring (1 / 1 / 2 per stage)
    points_group_sign: int = 1
    points_group_goal_diff: int = 1
    points_group_exact: int = 2

    points_r32_sign: int = 1
    points_r32_goal_diff: int = 1
    points_r32_exact: int = 2

    points_r16_sign: int = 1
    points_r16_goal_diff: int = 1
    points_r16_exact: int = 2

    points_qf_sign: int = 1
    points_qf_goal_diff: int = 1
    points_qf_exact: int = 2

    points_sf_sign: int = 1
    points_sf_goal_diff: int = 1
    points_sf_exact: int = 2

    points_3rd_sign: int = 1
    points_3rd_goal_diff: int = 1
    points_3rd_exact: int = 2

    points_final_sign: int = 1
    points_final_goal_diff: int = 1
    points_final_exact: int = 2

    points_group_rank_position: int = 1   # points per correct group finishing position (1st/2nd/3rd/4th)

    model_config = {"env_file": ".env", "extra": "ignore"}

    def stage_points(self, stage: str) -> tuple[int, int, int]:
        """Return (sign, goal_diff, exact) point values for the given API stage string."""
        return {
            "GROUP_STAGE":    (self.points_group_sign, self.points_group_goal_diff, self.points_group_exact),
            "LAST_32":        (self.points_r32_sign,   self.points_r32_goal_diff,   self.points_r32_exact),
            "LAST_16":        (self.points_r16_sign,   self.points_r16_goal_diff,   self.points_r16_exact),
            "QUARTER_FINALS": (self.points_qf_sign,    self.points_qf_goal_diff,    self.points_qf_exact),
            "SEMI_FINALS":    (self.points_sf_sign,    self.points_sf_goal_diff,    self.points_sf_exact),
            "THIRD_PLACE":    (self.points_3rd_sign,   self.points_3rd_goal_diff,   self.points_3rd_exact),
            "FINAL":          (self.points_final_sign, self.points_final_goal_diff, self.points_final_exact),
        }.get(stage, (self.points_group_sign, self.points_group_goal_diff, self.points_group_exact))


@lru_cache
def get_settings() -> Settings:
    return Settings()
