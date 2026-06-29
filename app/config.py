from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    football_data_api_key: str = ""
    database_url: str = "sqlite:///./data/app.db"
    poll_interval_seconds: int = 300
    score_recheck_delay_seconds: int = 600
    # Notify users about finished matches they did NOT predict (0 pts), but only
    # if the match finished within this many hours — avoids back-filling the whole
    # tournament's missed matches at once. Set to 0 to disable no-prediction notifications.
    no_prediction_max_age_hours: int = 48
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

    # Advancement bonus: pts awarded on top of sign/diff/exact when prediction is correct
    # SF = 2 because a correct SF winner also implicitly identifies the 3rd-place team (the loser)
    points_r32_advancement: int = 1
    points_r16_advancement: int = 1
    points_qf_advancement: int = 1
    points_sf_advancement: int = 2
    points_3rd_advancement: int = 1
    points_final_advancement: int = 5   # champion bonus
    points_final_runner_up: int = 3     # bonus for predicting the team that LOSES the Final

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

    def stage_advancement_points(self, stage: str) -> int:
        return {
            "LAST_32":        self.points_r32_advancement,
            "LAST_16":        self.points_r16_advancement,
            "QUARTER_FINALS": self.points_qf_advancement,
            "SEMI_FINALS":    self.points_sf_advancement,
            "THIRD_PLACE":    self.points_3rd_advancement,
            "FINAL":          self.points_final_advancement,
        }.get(stage, 0)

    def stage_runner_up_points(self, stage: str) -> int:
        return self.points_final_runner_up if stage == "FINAL" else 0


@lru_cache
def get_settings() -> Settings:
    return Settings()
