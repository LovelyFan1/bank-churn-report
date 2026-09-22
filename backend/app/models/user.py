"""用户与审计日志模型 —— 4A 里的「账号」与「审计」两件事。

**为什么是这两张表**

业内说的 4A = 账号(Account) / 认证(Authentication) / 授权(Authorization)
/ 审计(Audit)。本模块用两张表覆盖其中三件：

    users       账号 + 认证凭据 + 角色（授权依据）
    audit_logs  审计（谁、何时、做了什么、成没成）

**为什么把角色直接放在 users 上，而不是完整的 RBAC 三张表**

真银行的授权是「用户-角色-权限-资源」四层，因为权限点有几百个、
且要支持数据级授权（同一角色看不同分行）。本项目只有 8 个页面、
数据是单一份全量，四级模型会带来三张表和大量 join，
却演示不出额外价值。

故用**角色枚举**（三层：管理员 / 客户经理 / 只读），
权限点在代码里声明（见 auth_service.PERMISSIONS）。
这是**刻意简化**，不是遗漏 —— 答辩时若被问到要如实说这是简化。
"""

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base

# 角色常量 —— 与前端 auth store 的 ROLE_LABELS 对应
ROLE_ADMIN = "admin"          # 系统管理员：全部权限，含用户管理与审计查看
ROLE_MANAGER = "manager"      # 客户经理：查数据 + 建单 + 派单（日常业务角色）
ROLE_STAFF = "staff"          # 客户专员：只处理**指派给自己**的工单（执行岗）
ROLE_VIEWER = "viewer"        # 只读分析：只能看，不能建/改/删工单

ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_STAFF, ROLE_VIEWER)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # 行员号 —— 银行的账号就是工号（HR 系统为主数据源），不是邮箱。
    # 唯一性是等保「身份标识唯一性」的直接落地。
    username = Column(String(64), unique=True, index=True, nullable=False)
    display_name = Column(String(64))               # 姓名，界面上显示
    department = Column(String(64))                 # 部门（柜面/客户经理/风控…）

    # ── 认证凭据 ─────────────────────────────────────
    # ⚠ 只存 PBKDF2-HMAC-SHA256 的派生值，**绝不存明文**，
    #   也不存「可逆加密」——后者在密钥泄露时等同于明文。
    #   格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
    password_hash = Column(String(255), nullable=False)
    must_change_password = Column(Integer, default=0)  # 1 = 首次登录须改密

    # ── 双因素（认证的第二因子）───────────────────────
    # 真银行用 UKey/数字证书（等保要求"至少一种使用密码技术"）。
    # 本项目用 TOTP 动态口令仿真 —— 同样是**密码技术**，
    # 且不需要采购 CA 与 USB Key，演示环境可落地。
    totp_secret = Column(String(64))                # base32 密钥
    totp_enabled = Column(Integer, default=0)

    role = Column(String(16), default=ROLE_VIEWER)  # admin / manager / viewer
    is_active = Column(Integer, default=1)          # 0 = 停用（离职即停，不删）

    # ── 登录失败处理（等保三级明确要求）──────────────
    # 为什么要有这两列而不是放内存：多 worker（--workers 4）下内存计数
    # 各自为政，攻击者打 4 次只被记 1 次 —— 等于没有锁定。
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime)                 # 锁定到期时间；空=未锁定

    last_login_at = Column(DateTime)
    last_login_ip = Column(String(64))
    created_at = Column(DateTime, server_default=func.now())


class AuditLog(Base):
    """操作审计 —— 4A 的第四件事，也是本项目与 Agent 的接合点。

    ⚠ 为什么这张表对本项目特别重要：

      Agent 是「会自己调工具、甚至提议写库」的组件。上线当天就有一个
      真实缺口：`session_id` 硬编码为 `'ui'`，建单时 `assignee` 靠 LLM
      从用户话里抽 —— 于是**Agent 建的工单追不到是谁让建的**，
      而 LLM 抽出的负责人也可能是错的（用户没指定时它会编一个名字）。

      引入登录后，写操作的「操作者」由**服务端从会话取**，
      不再依赖模型输出。审计表把这件事留痕：
      「张三通过智能助手为 C071081 建单」—— 主体、动作、客体齐全。

    ⚠ 为什么记录的是 username 而不是 user_id 外键：
      离职员工被停用后，用户名仍是**当时的事实**；若只存外键，
      用户表一旦清理就失去可读性。审计要的是"当时发生了什么"，
      故冗余存一份可读标识（真银行审计系统同样如此）。
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), index=True)       # 操作者行员号（冗余存）
    display_name = Column(String(64))               # 操作者姓名
    role = Column(String(16))                       # 操作时的角色（事后提权也说得清）
    action = Column(String(64), index=True)         # login / logout / create_work_order …
    target = Column(String(128))                    # 操作对象，如 C071081 / #63
    detail = Column(Text)                           # JSON 附加信息（不落敏感字段）
    source = Column(String(32))                     # ui（页面） / agent（智能助手）
    success = Column(Integer, default=1)
    message = Column(String(255))                   # 失败原因 / 成功摘要
    ip = Column(String(64))
    created_at = Column(DateTime, server_default=func.now(), index=True)
